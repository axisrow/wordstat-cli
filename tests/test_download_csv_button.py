"""Issue #62: the CSV menu click must land on the inner button, not the anchor.

The menu item is ``<a download><button class="save-csv-button">``. The button's
React handler fetches ``getAllTableData`` and replaces the anchor's preliminary
header-only blob with the full-data blob before triggering the download.
``_click`` on the anchor dispatches the event on the anchor — it bubbles up
past the button, the handler never fires, and the anchor's native default
action downloads the stale header-only blob (the live root cause of #62's
empty top_popular/top_related exports).
"""

import asyncio

from wordstat.collector import (
    DOWNLOAD_CSV_BUTTON_SELECTOR,
    DOWNLOAD_CSV_MENU_ITEM_SELECTOR,
    WordstatCollector,
)
from wordstat.csv_io import parse_wordstat_csv
from wordstat.models import WordstatView


class _FakePage:
    async def evaluate(self, script, *args):
        return "true"


class _FakeSession:
    def __init__(self):
        self.downloaded_files = []


def test_download_current_view_clicks_the_csv_button_not_the_anchor(monkeypatch, tmp_path):
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    csv_file = downloads_path / "wordstat_top_queries.csv"
    clicks = []

    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=1, settling_seconds=0)

    async def click(self, page, selector):
        clicks.append(selector)
        if selector == DOWNLOAD_CSV_BUTTON_SELECTOR:
            # In the live UI the button's handler is what eventually produces
            # the file; model that ordering here.
            csv_file.write_text("Запросы;Число\nокна;100\n", encoding="utf-8")

    async def wait(self, page, expression, seconds=None, required=True):
        return None

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    source, escape_warning = asyncio.run(
        collector._download_current_view(_FakePage(), _FakeSession(), downloads_path)
    )

    # The menu-item anchor selector is only a *presence* gate; the click that
    # triggers the download must target the button inside it.
    assert DOWNLOAD_CSV_MENU_ITEM_SELECTOR not in clicks
    assert clicks == ["button.save-button", DOWNLOAD_CSV_BUTTON_SELECTOR]
    assert source == csv_file
    assert escape_warning is None


def test_csv_button_selector_targets_the_button_inside_the_anchor():
    # ``a[download] button`` — a descendant selector, so an event dispatched on
    # the match bubbles from the button upward, reaching the button's own
    # React handler. Regression-guarding the selector shape itself: someone
    # "simplifying" it back to the anchor re-opens issue #62 silently.
    assert DOWNLOAD_CSV_BUTTON_SELECTOR == "a[download] button.save-csv-button"


# --- issue #62 reopened (2026-09-10): the stale-blob race and the blind retry.

HEADER_ONLY = "Запросы;Число запросов;Топ частотных запросов «тест»\r"
FULL_EXPORT = "Запросы;Число запросов;Топ частотных запросов «тест»\rтест;100\r"


def test_poll_waits_for_the_full_csv_replacing_a_header_only_blob(tmp_path):
    """A top-view poll must not accept the instant header-only file.

    Live mechanism (reopened #62): the click can land the stale header-only
    blob immediately, and the real download then *replaces it under the same
    filename* once getAllTableData finishes. The poll's acceptance must be
    content-aware for top views and must see the in-place replacement (size/
    mtime change), not only new paths.
    """
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    csv_file = downloads_path / "wordstat_top_queries.csv"
    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=5, settling_seconds=0)

    async def scenario():
        poll = asyncio.create_task(
            collector._poll_current_view_download(
                _FakeSession(), downloads_path, {}, {}, set(),
                view=WordstatView.TOP_POPULAR,
            )
        )
        csv_file.write_text(HEADER_ONLY, encoding="utf-8")
        await asyncio.sleep(0.6)
        csv_file.write_text(FULL_EXPORT, encoding="utf-8")
        return await poll

    source, _ = asyncio.run(scenario())
    assert source == csv_file
    assert "тест;100" in source.read_text(encoding="utf-8")


def test_poll_returns_header_only_top_csv_at_the_deadline(tmp_path):
    """A header-only file that is never replaced is still returned honestly.

    Wordstat can legitimately produce an empty top export (getAllTableData
    returned no data); the poll waits for a replacement until the deadline
    and then falls back to the header-only file so the existing
    retry/partial machinery downstream owns that case.
    """
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    csv_file = downloads_path / "wordstat_top_queries.csv"
    csv_file.write_text(HEADER_ONLY, encoding="utf-8")
    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=1, settling_seconds=0)

    source, _ = asyncio.run(
        collector._poll_current_view_download(
            _FakeSession(), downloads_path, {}, {}, set(),
            view=WordstatView.TOP_POPULAR,
        )
    )
    assert source == csv_file


def test_poll_accepts_a_non_top_view_file_immediately(tmp_path):
    """Non-top views keep the old size-only acceptance: no content parsing."""
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    csv_file = downloads_path / "wordstat_regions.csv"
    csv_file.write_text("Регион;Показов\r", encoding="utf-8")
    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=5, settling_seconds=0)

    source, _ = asyncio.run(
        collector._poll_current_view_download(
            _FakeSession(), downloads_path, {}, {}, set(),
            view=WordstatView.REGIONS,
        )
    )
    assert source == csv_file


def test_retry_removes_the_stale_file_so_a_same_name_redownload_is_visible(monkeypatch, tmp_path):
    """The retry must unlink the backed-up empty file before re-clicking.

    Before this fix the retry re-clicked with the old file still in the
    downloads directory: a same-name re-download produced no new path, the
    poll raised DownloadNoNewPathError, and the code restored the empty
    backup — silently discarding the full export Chrome had just written
    over the same filename (the 2026-09-10 live recurrence of #62).
    """
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()
    source = downloads_path / "wordstat_top_queries.csv"
    source.write_text(HEADER_ONLY, encoding="utf-8")
    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=1, settling_seconds=0,
                                  empty_export_retry_seconds=0)
    observed = {}

    async def fake_download(self, page, session, dl_path, view=None):
        observed["source_existed_at_recall"] = source.exists()
        observed["view"] = view
        # Chrome overwrites the same filename with the full export.
        source.write_text(FULL_EXPORT, encoding="utf-8")
        return source, None

    monkeypatch.setattr(WordstatCollector, "_download_current_view", fake_download)
    dataset = parse_wordstat_csv(source, WordstatView.TOP_POPULAR)

    retry = asyncio.run(
        collector._retry_empty_export(
            _FakePage(), _FakeSession(), downloads_path, source, dataset,
            WordstatView.TOP_POPULAR, [],
        )
    )
    assert observed["source_existed_at_recall"] is False
    assert observed["view"] is WordstatView.TOP_POPULAR
    assert retry.source == source
    assert len(retry.dataset.rows) == 1
    assert not list(tmp_path.glob("*retry*"))
