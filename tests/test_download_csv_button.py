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
