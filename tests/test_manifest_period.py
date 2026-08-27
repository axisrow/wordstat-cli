from datetime import date

import pytest

from wordstat.collector import WordstatCollector
from wordstat.errors import InterfaceChangedError
from wordstat.models import CsvDataset, WordstatView
from wordstat.periods import Granularity


def test_actual_period_uses_the_values_present_in_the_export():
    dataset = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Дата", "Число запросов"],
        rows=[
            {"Дата": "22.06.2026", "Число запросов": "1"},
            {"Дата": "18.08.2026", "Число запросов": "2"},
        ],
    )

    assert WordstatCollector._actual_period(dataset) == {
        "field": "Дата",
        "from": "22.06.2026",
        "to": "18.08.2026",
    }


def test_dynamics_filename_identifies_non_monthly_granularity():
    assert WordstatCollector._dynamics_file_name(Granularity.MONTHLY) == "dynamics.parquet"
    assert WordstatCollector._dynamics_file_name(Granularity.DAILY) == "dynamics_daily.parquet"
    assert WordstatCollector._dynamics_file_name(Granularity.WEEKLY) == "dynamics_weekly.parquet"


def test_dynamics_series_allows_short_tail_but_rejects_internal_gap():
    dataset = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Дата"],
        rows=[{"Дата": "22.06.2026"}, {"Дата": "23.06.2026"}, {"Дата": "25.06.2026"}],
    )

    with pytest.raises(InterfaceChangedError, match="gap"):
        WordstatCollector._assert_contiguous_dynamics_rows(dataset, Granularity.DAILY)

    short_tail = dataset.model_copy(update={"rows": [{"Дата": "22.06.2026"}, {"Дата": "23.06.2026"}]})
    WordstatCollector._assert_contiguous_dynamics_rows(short_tail, Granularity.DAILY)


def test_dynamics_series_rejects_a_stale_window_that_predates_the_request():
    # A readiness gate that only confirmed the first row matched date_from
    # could pass for a table that still shows an earlier, stale window
    # whose *first* row happens to coincide with date_from by chance, while
    # its last row never advanced to cover date_to. This exercises the
    # containment check directly on the exported rows (the actual defense
    # against a stale end boundary), independent of the live DOM race that
    # motivated it (collector.py:603).
    dataset = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Дата"],
        rows=[
            {"Дата": "22.06.2026"},
            {"Дата": "23.06.2026"},
            {"Дата": "24.06.2026"},
        ],
    )
    # A shorter export fully inside the requested window is legitimate
    # (documented variable trailing tail) and must not be rejected.
    WordstatCollector._assert_contiguous_dynamics_rows(
        dataset, Granularity.DAILY, date_from=date(2026, 6, 22), date_to=date(2026, 8, 20)
    )
    # A row before the requested start is never legitimate: it means the
    # table had not advanced past a previous, stale window.
    with pytest.raises(InterfaceChangedError, match="requested window"):
        WordstatCollector._assert_contiguous_dynamics_rows(
            dataset, Granularity.DAILY, date_from=date(2026, 6, 23), date_to=date(2026, 8, 20)
        )
    # A row after the requested end is never legitimate either: it means
    # the table is still showing a different, later window than requested.
    with pytest.raises(InterfaceChangedError, match="requested window"):
        WordstatCollector._assert_contiguous_dynamics_rows(
            dataset, Granularity.DAILY, date_from=date(2026, 6, 22), date_to=date(2026, 6, 23)
        )


def test_single_row_export_is_not_exempt_from_the_containment_check():
    # Cycle-review round 2, C2: the containment check originally sat behind
    # the same `len(rows) < 2` early return as the pairwise contiguity loop
    # -- but containment only needs dates[0]/dates[-1] (the same row, for a
    # 1-row export), not a pair. A genuine 1-row daily/weekly export whose
    # single date lies entirely outside the requested window passed
    # _is_untrustworthy_empty_export (which only rejects 0 rows) and was
    # then written to Parquet and marked complete, unguarded.
    stale_single_row = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Дата"],
        rows=[{"Дата": "20.06.2026"}],
    )
    with pytest.raises(InterfaceChangedError, match="requested window"):
        WordstatCollector._assert_contiguous_dynamics_rows(
            stale_single_row, Granularity.DAILY, date_from=date(2026, 6, 22), date_to=date(2026, 8, 20)
        )
    # A single row genuinely inside the requested window remains legitimate
    # (e.g. a one-day window, or the daily series' documented variable
    # trailing tail collapsed to one row) and must not be rejected.
    in_window_single_row = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Дата"],
        rows=[{"Дата": "22.06.2026"}],
    )
    WordstatCollector._assert_contiguous_dynamics_rows(
        in_window_single_row, Granularity.DAILY, date_from=date(2026, 6, 22), date_to=date(2026, 8, 20)
    )
    # No explicit period requested (the UI's default window) still skips
    # the check entirely -- nothing to validate a single row against.
    WordstatCollector._assert_contiguous_dynamics_rows(stale_single_row, Granularity.DAILY)


def test_weekly_containment_uses_the_aligned_week_start_not_the_raw_request():
    # Wordstat snaps a weekly window's first row to the Monday of
    # date_from's week (confirmed live: 2018-12-26, a Wednesday, produced
    # a first cell starting 24.12.2018 -- the preceding Monday). A
    # containment check that compared against the raw date_from instead of
    # that aligned Monday would misreport this legitimate alignment as a
    # stale window.
    dataset = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Неделя с"],
        rows=[{"Неделя с": "24.12.2018"}, {"Неделя с": "31.12.2018"}],
    )
    WordstatCollector._assert_contiguous_dynamics_rows(
        dataset, Granularity.WEEKLY, date_from=date(2018, 12, 26), date_to=date(2019, 1, 13)
    )


def test_monthly_containment_uses_actual_month_boundaries():
    dataset = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Период"],
        rows=[
            {"Период": "июнь 2026"},
            {"Период": "июль 2026"},
            {"Период": "август 2026"},
        ],
    )

    with pytest.raises(InterfaceChangedError, match="requested window"):
        WordstatCollector._assert_contiguous_dynamics_rows(
            dataset, Granularity.MONTHLY, date_from=date(2026, 7, 15), date_to=date(2026, 8, 20)
        )

    with pytest.raises(InterfaceChangedError, match="requested window"):
        WordstatCollector._assert_contiguous_dynamics_rows(
            dataset, Granularity.MONTHLY, date_from=date(2026, 6, 1), date_to=date(2026, 7, 31)
        )

    # Date requests are day-granular, but monthly rows are compared by their
    # containing months, so the June-August export is inside this window.
    WordstatCollector._assert_contiguous_dynamics_rows(
        dataset, Granularity.MONTHLY, date_from=date(2026, 6, 15), date_to=date(2026, 8, 20)
    )


def test_monthly_contiguity_rejects_a_missing_month():
    dataset = CsvDataset(
        view=WordstatView.DYNAMICS,
        headers=["Период"],
        rows=[{"Период": "декабрь 2023"}, {"Период": "февраль 2024"}],
    )

    with pytest.raises(InterfaceChangedError, match="gap"):
        WordstatCollector._assert_contiguous_dynamics_rows(dataset, Granularity.MONTHLY)
