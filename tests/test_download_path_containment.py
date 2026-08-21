"""Issue #27: a download landing outside downloads_path must never be moved
or deleted — it may be a stray path in ~/Downloads, a user's file.

session.downloaded_files is populated by browser-use from CDP events across
the whole browser session; nothing about it guarantees a path stays inside
the downloads_path this collector actually configured. _resolved_file_snapshot
used to union that list in blindly, so a stray/escaped path was silently
treated as "the new download" and handed to finalize_raw, which then either
crashed trying to replace() across a TCC-protected directory (~/Downloads on
macOS: Errno 1 Operation not permitted) or, worse, without --keep-raw would
have unlink()ed a file the tool has no business touching.
"""

import asyncio

import pytest

from wordstat.collector import WordstatCollector
from wordstat.errors import DownloadEscapedError


class _FakePage:
    async def evaluate(self, script, *args):
        # Both the "click download button" and "wait for CSV menu item" steps
        # only need to not raise; the CSV click below is what matters.
        return "true"


class _FakeSessionWithStrayDownload:
    """Starts with no downloads reported; a stray path outside downloads_path
    is only added once the CSV menu item is clicked — mirrors the live
    issue #27 symptom, where the escaped download only appears after the
    export click, not before it."""

    def __init__(self, stray_path):
        self.downloaded_files = []
        self._stray_path = str(stray_path)

    def report_stray_download(self):
        self.downloaded_files.append(self._stray_path)


def test_download_current_view_refuses_a_file_outside_downloads_path(monkeypatch, tmp_path):
    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    # Simulates a file Chrome saved to ~/Downloads (or anywhere else outside
    # our own downloads_path) instead of honoring downloads_path.
    stray_dir = tmp_path / "not-ours" / "Downloads"
    stray_dir.mkdir(parents=True)
    stray_file = stray_dir / "wordstat_regions.csv"
    stray_file.write_text("Регион;Показов\nМосква;100\n", encoding="cp1251")

    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=1, settling_seconds=0)

    session = _FakeSessionWithStrayDownload(stray_file)

    async def click(self, page, selector):
        if selector == "button.save-button":
            # The second click (on the CSV menu item) is what triggers the
            # actual download in the real UI; report the stray path only
            # after that click, matching the real event ordering.
            return None
        session.report_stray_download()

    monkeypatch.setattr(WordstatCollector, "_click", click)

    async def run():
        await collector._download_current_view(_FakePage(), session, downloads_path)

    with pytest.raises(DownloadEscapedError, match=str(stray_file)):
        asyncio.run(run())

    # The stray file must be left exactly where it was — never moved, never
    # deleted, regardless of --keep-raw.
    assert stray_file.exists()
    assert stray_file.read_text(encoding="cp1251") == "Регион;Показов\nМосква;100\n"


class _FakeSessionWithSimultaneousStrayDownload:
    """Reports the stray path in the SAME tick the legitimate CSV appears —
    the click handler drops both files before the first poll iteration runs,
    so _resolved_file_snapshot and _escaped_download_paths both see the full
    picture on their very first call. Regression coverage for the cycle-review
    follow-up to issue #27: session.downloaded_files is session-lifetime and
    append-only, so a stray path masked by a same-tick success here would
    never surface as new_escaped on any later call either — this is the only
    tick in which the escape is observable at all."""

    def __init__(self, stray_path):
        self.downloaded_files = []
        self._stray_path = str(stray_path)

    def report_stray_download(self):
        self.downloaded_files.append(self._stray_path)


def test_download_current_view_warns_but_succeeds_when_escape_and_legitimate_csv_share_a_poll_tick(
    monkeypatch, tmp_path
):
    """A stray escaped download that lands in the exact same poll tick as the
    view's own legitimate CSV must not fail the view — the view's data is
    safely on disk — but the operator must still be told about the stray file
    via the returned warning, since this is the only tick in which the escape
    is detectable at all (session.downloaded_files never resets, so it would
    be silently absorbed into next call's baseline and never surface later)."""

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    stray_dir = tmp_path / "not-ours" / "Downloads"
    stray_dir.mkdir(parents=True)
    stray_file = stray_dir / "wordstat_regions.csv"
    stray_file.write_text("Регион;Показов\nМосква;100\n", encoding="cp1251")

    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=1, settling_seconds=0)
    session = _FakeSessionWithSimultaneousStrayDownload(stray_file)

    async def click(self, page, selector):
        if selector == "button.save-button":
            return None
        # Both the legitimate CSV and the stray path land before the first
        # poll iteration observes either — same tick, by construction.
        legit = downloads_path / "wordstat_top_queries.csv"
        legit.write_text("Запрос;Показов\nремонт;1000\n", encoding="cp1251")
        session.report_stray_download()

    monkeypatch.setattr(WordstatCollector, "_click", click)

    async def run():
        return await collector._download_current_view(_FakePage(), session, downloads_path)

    path, warning = asyncio.run(run())

    assert path.name == "wordstat_top_queries.csv"
    assert path.read_text(encoding="cp1251") == "Запрос;Показов\nремонт;1000\n"
    assert warning is not None
    assert str(stray_file) in warning

    # The stray file must still be left exactly where it was.
    assert stray_file.exists()
    assert stray_file.read_text(encoding="cp1251") == "Регион;Показов\nМосква;100\n"


def test_download_current_view_returns_no_warning_on_the_common_path(monkeypatch, tmp_path):
    """No escape at all: the warning slot must be None, not an empty string
    or some other falsy-but-present sentinel — callers branch on `is not
    None`."""

    downloads_path = tmp_path / "downloads"
    downloads_path.mkdir()

    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=1, settling_seconds=0)

    class _FakeSessionNoDownloads:
        downloaded_files: list[str] = []

    session = _FakeSessionNoDownloads()

    async def click(self, page, selector):
        if selector != "button.save-button":
            (downloads_path / "wordstat_top_queries.csv").write_text(
                "Запрос;Показов\nремонт;1000\n", encoding="cp1251"
            )

    monkeypatch.setattr(WordstatCollector, "_click", click)

    async def run():
        return await collector._download_current_view(_FakePage(), session, downloads_path)

    path, warning = asyncio.run(run())

    assert path.name == "wordstat_top_queries.csv"
    assert warning is None
