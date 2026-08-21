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
