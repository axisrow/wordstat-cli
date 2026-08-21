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
