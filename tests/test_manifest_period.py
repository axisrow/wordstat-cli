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
