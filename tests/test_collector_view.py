"""View-selection retry behavior without touching a browser."""

import asyncio

import pytest

from wordstat.collector import WordstatCollector, _is_untrustworthy_empty_export
from wordstat.errors import InterfaceChangedError
from wordstat.models import CsvDataset, WordstatView


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

    asyncio.run(WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='map']", WordstatView.REGIONS))

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

    async def snapshot(self, page):
        return None

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)
    monkeypatch.setattr(WordstatCollector, "_table_snapshot", snapshot)

    with pytest.raises(InterfaceChangedError, match="view did not change"):
        asyncio.run(
            WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='graph']", WordstatView.DYNAMICS)
        )

    assert clicks == 2


def test_select_view_splits_timeout_budget_across_retries(monkeypatch, tmp_path):
    # Regression guard: two attempts must share the configured timeout, not
    # each get a full copy of it (found in review — a stuck view used to take
    # ~2x self.timeout_seconds instead of ~1x before raising).
    seconds_seen = []

    async def click(self, page, selector):
        pass

    async def wait(self, page, expression, seconds=None, required=True):
        seconds_seen.append(seconds)
        raise InterfaceChangedError("view did not change")

    async def snapshot(self, page):
        return None

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)
    monkeypatch.setattr(WordstatCollector, "_table_snapshot", snapshot)

    collector = WordstatCollector("cdp", tmp_path, timeout_seconds=45.0)
    with pytest.raises(InterfaceChangedError):
        asyncio.run(collector._select_view(object(), "label[for='graph']", WordstatView.DYNAMICS))

    assert seconds_seen == [22.5, 22.5]


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

    async def snapshot(self, page):
        return "previous row text"

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)
    monkeypatch.setattr(WordstatCollector, "_table_snapshot", snapshot)

    asyncio.run(
        WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='table']", WordstatView.TOP_POPULAR)
    )

    assert ".table__wrapper" in waits[0]


def test_select_view_keeps_content_change_out_of_the_hard_gate(monkeypatch, tmp_path):
    # Table text is helpful evidence of a repaint, but different views may
    # legitimately have equal first rows. The required gate must remain only
    # checked + download + rows; the content check follows as best-effort.
    waits = []

    async def click(self, page, selector):
        pass

    async def wait(self, page, expression, seconds=None, required=True):
        waits.append((expression, seconds, required))

    async def snapshot(self, page):
        return "same first row in two views"

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)
    monkeypatch.setattr(WordstatCollector, "_table_snapshot", snapshot)

    asyncio.run(
        WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='table']", WordstatView.TOP_POPULAR)
    )

    hard_expression, _, hard_required = waits[0]
    soft_expression, soft_seconds, soft_required = waits[1]
    assert ".table__wrapper" in hard_expression
    assert "same first row in two views" not in hard_expression
    assert "!==" not in hard_expression
    assert hard_required is True
    assert "same first row in two views" in soft_expression
    assert "!==" in soft_expression
    assert soft_seconds == 3.0
    assert soft_required is False


def _dataset(view: WordstatView, rows: list[dict[str, str]]) -> CsvDataset:
    return CsvDataset(view=view, headers=["query", "count"], rows=rows)


@pytest.mark.parametrize(
    "view", [WordstatView.TOP_POPULAR, WordstatView.TOP_RELATED, WordstatView.DYNAMICS]
)
def test_empty_export_is_untrustworthy_for_table_views_regardless_of_dom_state(view):
    # Regression guard for issue #11 (and its cycle-2 follow-up): _select_view
    # already hard-gates TABLE_ROW_SELECTOR.length > 0 on the DOM before
    # "Скачать" is ever clicked for every view but REGIONS, so an empty CSV
    # reaching this point is already a contradiction for all three of these
    # table-based views — it must be rejected unconditionally, with no
    # second, later DOM read able to wave it through as "legitimately empty"
    # (that re-read can observe a table that has since emptied and silently
    # accept a corrupted export — the exact bug this predicate replaces).
    assert _is_untrustworthy_empty_export(view, _dataset(view, [])) is True


@pytest.mark.parametrize(
    "view", [WordstatView.TOP_POPULAR, WordstatView.TOP_RELATED, WordstatView.DYNAMICS]
)
def test_non_empty_export_is_trusted_for_table_views(view):
    assert _is_untrustworthy_empty_export(view, _dataset(view, [{"query": "a", "count": "1"}])) is False


def test_empty_regions_export_is_not_flagged_by_this_gate():
    # regions (map view) has no table in its DOM at all.
    assert _is_untrustworthy_empty_export(WordstatView.REGIONS, _dataset(WordstatView.REGIONS, [])) is False


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

    asyncio.run(WordstatCollector("cdp", tmp_path)._select_view(object(), "label[for='map']", WordstatView.REGIONS))

    assert ".table__wrapper" not in waits[0]
