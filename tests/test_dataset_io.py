"""Parquet writing for parsed Wordstat exports."""

from pathlib import Path

import pyarrow.parquet as pq

from wordstat.dataset_io import write_dataset
from wordstat.models import CsvDataset, WordstatView


def _dataset(headers, rows, view=WordstatView.TOP_POPULAR):
    return CsvDataset(view=view, source_file=Path("ignored.csv"), headers=headers, rows=rows)


def test_write_dataset_types_numeric_columns_and_keeps_text(tmp_path):
    dataset = _dataset(
        ["Запрос", "Показов", "Доля"],
        [
            {"Запрос": "ремонт квартир", "Показов": "5 228 679", "Доля": "12,5%"},
            {"Запрос": "ремонт", "Показов": "1 020", "Доля": "3,1%"},
        ],
    )

    path, dtypes = write_dataset(dataset, tmp_path)

    assert path == tmp_path / "top_popular.parquet"
    assert dtypes == {"Запрос": "string", "Показов": "int64", "Доля": "string"}

    table = pq.read_table(path)
    assert table.column_names == ["Запрос", "Показов", "Доля"]
    assert table.to_pylist() == [
        {"Запрос": "ремонт квартир", "Показов": 5228679, "Доля": "12,5%"},
        {"Запрос": "ремонт", "Показов": 1020, "Доля": "3,1%"},
    ]


def test_write_dataset_writes_empty_cells_as_null(tmp_path):
    dataset = _dataset(
        ["Регион", "Показов"],
        [{"Регион": "Москва", "Показов": "100"}, {"Регион": "Тула", "Показов": ""}],
        view=WordstatView.REGIONS,
    )

    path, dtypes = write_dataset(dataset, tmp_path)

    assert dtypes["Показов"] == "int64"
    assert pq.read_table(path).to_pylist() == [
        {"Регион": "Москва", "Показов": 100},
        {"Регион": "Тула", "Показов": None},
    ]


def test_write_dataset_keeps_dynamics_periods_as_text(tmp_path):
    dataset = _dataset(
        ["Период", "Показов"],
        [{"Период": "01.2024", "Показов": "10"}, {"Период": "02.2024", "Показов": "20"}],
        view=WordstatView.DYNAMICS,
    )

    _, dtypes = write_dataset(dataset, tmp_path)

    assert dtypes["Период"] == "string"


def test_write_dataset_handles_a_report_without_rows(tmp_path):
    dataset = _dataset(["Запрос", "Показов"], [])

    path, dtypes = write_dataset(dataset, tmp_path)

    assert dtypes == {"Запрос": "string", "Показов": "string"}
    assert pq.read_table(path).to_pylist() == []
