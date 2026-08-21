"""View-selection retry behavior without touching a browser."""

import asyncio
import re
from datetime import date

import pytest

from wordstat.collector import WordstatCollector, _is_untrustworthy_empty_export
from wordstat.errors import InterfaceChangedError
from wordstat.models import CsvDataset, WordstatView
from wordstat.periods import Granularity


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


# _wait_for_period_applied's regex must match the live table's actual text.
# Daily/weekly first cells are genitive ("22 июня", "24 декабря 2018" — "of
# June", "of December"), unlike the nominative RUSSIAN_MONTHS list used for
# clicking the calendar popups ("июнь", "декабрь"). A live CDP check (issue
# #6 phase 2) confirmed a fixed nominative-month match against the live
# cell's genitive text never matches, so _wait_for ran out its full timeout
# every time instead of detecting the period was actually already applied.
# These are regression guards for that exact mismatch, not for the general
# _wait_for polling mechanism (already covered above).
def _captured_pattern(monkeypatch, tmp_path):
    captured = {}

    async def wait(self, page, expression, seconds=None, required=True):
        captured["expression"] = expression

    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)
    return WordstatCollector("cdp", tmp_path), captured


def _extract_regex(expression: str) -> re.Pattern:
    # _wait_for_period_applied builds "new RegExp(<json-encoded pattern>)...".
    # Extract and compile the same pattern the live page would receive.
    import json

    start = expression.index("new RegExp(") + len("new RegExp(")
    end = expression.index(")", start)
    pattern = json.loads(expression[start:end])
    return re.compile(pattern)


def test_wait_for_period_applied_daily_matches_genitive_cell_text(monkeypatch, tmp_path):
    collector, captured = _captured_pattern(monkeypatch, tmp_path)
    asyncio.run(collector._wait_for_period_applied(object(), Granularity.DAILY, date(2026, 6, 22)))
    pattern = _extract_regex(captured["expression"])
    assert pattern.search("22 июня")
    assert not pattern.search("23 июня")
    # The month must match exactly, not just "some month word" — a stuck
    # calendar that landed on the right day of a wrong month must still
    # fail this check (found in review: an early version anchored only on
    # day+year and would have silently accepted this).
    assert not pattern.search("22 июля")


def test_wait_for_period_applied_weekly_matches_genitive_cell_text_and_aligns_to_monday(monkeypatch, tmp_path):
    collector, captured = _captured_pattern(monkeypatch, tmp_path)
    # 2018-12-26 is a Wednesday; the first cell is the Monday of that week.
    asyncio.run(collector._wait_for_period_applied(object(), Granularity.WEEKLY, date(2018, 12, 26)))
    pattern = _extract_regex(captured["expression"])
    assert pattern.search("24 декабря 2018 – 30 декабря 2018")
    assert not pattern.search("31 декабря 2018 – 6 января 2019")
    assert not pattern.search("24 ноября 2018 – 30 ноября 2018")


def test_wait_for_period_applied_monthly_matches_nominative_cell_text(monkeypatch, tmp_path):
    collector, captured = _captured_pattern(monkeypatch, tmp_path)
    asyncio.run(collector._wait_for_period_applied(object(), Granularity.MONTHLY, date(2024, 8, 1)))
    pattern = _extract_regex(captured["expression"])
    assert pattern.search("август 2024")
    assert not pattern.search("сентябрь 2024")


def test_set_period_does_not_reopen_the_monthly_popup_between_dates(monkeypatch, tmp_path):
    # Live CDP check (issue #6 phase 2, cycle-review round 2): the monthly
    # calendar is a single range-picker, not two independent popups like
    # day/week. Live DOM evidence: right after date_from is picked, every
    # month element already carries the "in-selecting-range" class (the
    # picker is already in range-selection mode), and the date-range
    # button's text does not update to reflect date_from yet -- it only
    # updates once date_to is picked in the SAME still-open popup. The
    # intermediate DATE_RANGE_SELECTOR click that day/week rely on to
    # reopen their popup instead closes this one (confirmed live:
    # visibility flips to 'hidden'), so the follow-up _select_calendar_date
    # call for date_to times out waiting for a popup that never reappears.
    # Live-confirmed fix: for monthly, skip the intermediate click and let
    # the second _select_calendar_date pick date_to in the still-open
    # popup -- confirmed live to produce the correct button text
    # "Январь 2024 — Июнь 2024".
    calls = []

    async def click(self, page, selector):
        calls.append(("click", selector))

    async def select_calendar_date(self, page, popup_type, target):
        calls.append(("select_calendar_date", popup_type, target))

    async def wait_for_period_applied(self, page, granularity, date_from):
        calls.append(("wait_for_period_applied", granularity, date_from))

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_select_calendar_date", select_calendar_date)
    monkeypatch.setattr(WordstatCollector, "_wait_for_period_applied", wait_for_period_applied)

    collector = WordstatCollector("cdp", tmp_path)
    asyncio.run(
        collector._set_period(object(), Granularity.MONTHLY, date(2024, 1, 1), date(2024, 6, 30))
    )

    click_count = sum(1 for call in calls if call[0] == "click")
    assert click_count == 1, f"expected exactly one popup-open click for monthly, got {calls}"
    select_calls = [call for call in calls if call[0] == "select_calendar_date"]
    assert select_calls == [
        ("select_calendar_date", "month", date(2024, 1, 1)),
        ("select_calendar_date", "month", date(2024, 6, 30)),
    ]


def test_set_period_still_reopens_the_popup_between_dates_for_day_and_week(monkeypatch, tmp_path):
    # day/week use independent popups per click (confirmed live in round 1:
    # 255bca7's live weekly/daily runs both went through this two-click
    # path successfully) -- only monthly's shared range-picker changes.
    calls = []

    async def click(self, page, selector):
        calls.append("click")

    async def select_calendar_date(self, page, popup_type, target):
        pass

    async def wait_for_period_applied(self, page, granularity, date_from):
        pass

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_select_calendar_date", select_calendar_date)
    monkeypatch.setattr(WordstatCollector, "_wait_for_period_applied", wait_for_period_applied)

    collector = WordstatCollector("cdp", tmp_path)
    asyncio.run(
        collector._set_period(object(), Granularity.DAILY, date(2026, 7, 1), date(2026, 8, 20))
    )
    assert len(calls) == 2

    calls.clear()
    asyncio.run(
        collector._set_period(object(), Granularity.WEEKLY, date(2026, 7, 1), date(2026, 8, 20))
    )
    assert len(calls) == 2


def test_select_calendar_date_clicks_the_month_popup_text_as_the_dom_renders_it(monkeypatch, tmp_path):
    # Live CDP check (issue #6 phase 2, cycle-review round 2): the monthly
    # calendar's month-text popup (.react-datepicker__month-text) renders
    # lowercase nominative month names ("январь"), matching RUSSIAN_MONTHS
    # verbatim -- confirmed by reading the popup's actual elements live.
    # month_label.capitalize() ("Январь") never matches any of them, so
    # every explicit --granularity monthly --date-from/--date-to request
    # failed with InterfaceChangedError: "Wordstat option 'Январь' was not
    # uniquely found" deep inside this click, despite validate_period
    # accepting the request and the browser already being driven.
    clicked_texts = []

    async def click(self, page, selector):
        pass

    async def wait(self, page, expression, seconds=None, required=True):
        pass

    async def click_visible_text(self, page, selector, text):
        clicked_texts.append((selector, text))

    async def click_visible_date_button(self, page, selector, text):
        clicked_texts.append((selector, text))

    monkeypatch.setattr(WordstatCollector, "_click", click)
    monkeypatch.setattr(WordstatCollector, "_wait_for", wait)
    monkeypatch.setattr(WordstatCollector, "_click_visible_text", click_visible_text)
    monkeypatch.setattr(WordstatCollector, "_click_visible_date_button", click_visible_date_button)

    collector = WordstatCollector("cdp", tmp_path)
    asyncio.run(collector._select_calendar_date(object(), "month", date(2024, 1, 1)))

    month_click = next(
        text for selector, text in clicked_texts if "month" in selector or "react-datepicker" in selector
    )
    assert month_click == "январь"
