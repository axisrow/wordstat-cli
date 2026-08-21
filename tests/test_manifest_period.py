from wordstat.collector import WordstatCollector
from wordstat.models import CsvDataset, WordstatView


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
    from wordstat.periods import Granularity

    assert WordstatCollector._dynamics_file_name(Granularity.MONTHLY) == "dynamics.parquet"
    assert WordstatCollector._dynamics_file_name(Granularity.DAILY) == "dynamics_daily.parquet"
    assert WordstatCollector._dynamics_file_name(Granularity.WEEKLY) == "dynamics_weekly.parquet"
