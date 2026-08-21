"""View-selection retry behavior without touching a browser."""

import asyncio

import pytest

from wordstat.collector import WordstatCollector
from wordstat.errors import InterfaceChangedError
from wordstat.models import WordstatView


def test_select_view_retries_once_when_active_marker_does_not_change(monkeypatch, tmp_path):
    clicks = []
    waits = []

    async def click(self, page, selector):
        clicks.append(selector)

    async def wait(self, page, expression, seconds=None, required=True):
        waits.append((expression, seconds, required))
        if len(waits) == 1:
            raise InterfaceChangedError("view did not change")

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    asyncio.run(
        WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='map']", WordstatView.REGIONS)
    )

    assert clicks == ["label[for='map']", "label[for='map']"]
    assert len(waits) == 2
    assert "input" in waits[0][0]


def test_select_view_raises_after_one_retry(monkeypatch, tmp_path):
    clicks = 0

    async def click(self, page, selector):
        nonlocal clicks
        clicks += 1

    async def wait(self, page, expression, seconds=None, required=True):
        raise InterfaceChangedError("view did not change")

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    with pytest.raises(InterfaceChangedError, match="view did not change"):
        asyncio.run(
            WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='graph']", WordstatView.DYNAMICS)
        )

    assert clicks == 2


def test_select_view_waits_for_table_rows_on_table_based_views(monkeypatch, tmp_path):
    # Regression guard: the readiness check must still require at least one
    # rendered table row for table-based views (top_popular, top_related,
    # dynamics), not just the active radio marker + download button. The
    # radio can flip to checked before Wordstat re-paints the table, and
    # downloading in that window silently produces a header-only CSV
    # (row_count: 0) — see issue #3's own warning and CLAUDE.md's known-issue
    # note. The map view (WordstatView.REGIONS) has no table and is exempt.
    waits = []

    async def click(self, page, selector):
        pass

    async def wait(self, page, expression, seconds=None, required=True):
        waits.append(expression)

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    asyncio.run(
        WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='table']", WordstatView.TOP_POPULAR)
    )

    assert ".table__wrapper" in waits[0]


def test_select_view_does_not_wait_for_table_rows_on_map_view(monkeypatch, tmp_path):
    # The map view (WordstatView.REGIONS) has no table rows in its DOM at
    # all, so gating on row presence would hang forever; it must only wait
    # on the radio marker + download button.
    waits = []

    async def click(self, page, selector):
        pass

    async def wait(self, page, expression, seconds=None, required=True):
        waits.append(expression)

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)

    asyncio.run(
        WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='map']", WordstatView.REGIONS)
    )

    assert ".table__wrapper" not in waits[0]
