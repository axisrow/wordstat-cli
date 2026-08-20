from pathlib import Path

import pytest

from wordstat.csv_io import parse_wordstat_csv
from wordstat.errors import CsvFormatError
from wordstat.models import WordstatView


def test_parse_wordstat_csv_preserves_localized_headers(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    source.write_bytes("Запрос;Число запросов\nчай;5 228 679\n".encode("cp1251"))

    dataset = parse_wordstat_csv(source, WordstatView.TOP_POPULAR)

    assert dataset.headers == ["Запрос", "Число запросов"]
    assert dataset.rows == [{"Запрос": "чай", "Число запросов": "5 228 679"}]


def test_parse_wordstat_csv_reads_a_tab_delimited_export(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    source.write_text("Запрос\tЧисло запросов\nчай\t5228679\n", encoding="utf-8")

    dataset = parse_wordstat_csv(source, WordstatView.TOP_POPULAR)

    assert dataset.headers == ["Запрос", "Число запросов"]
    assert dataset.rows == [{"Запрос": "чай", "Число запросов": "5228679"}]


def test_parse_wordstat_csv_rejects_duplicate_headers(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    source.write_text("query,query\none,two\n", encoding="utf-8")

    with pytest.raises(CsvFormatError, match="duplicate headers"):
        parse_wordstat_csv(source, WordstatView.TOP_POPULAR)
