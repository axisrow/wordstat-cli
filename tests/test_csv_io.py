from pathlib import Path

import pytest

from wordstat.csv_io import _validate_headers, parse_wordstat_csv
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


def test_parse_wordstat_csv_reads_header_only_semicolon_export(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    source.write_text(
        "Запросы со словами;Число запросов;"
        "Топ частотных запросов «новогодние подарки», 20.07.2026 — 20.08.2026, Россия, все устройства",
        encoding="utf-8",
    )

    dataset = parse_wordstat_csv(source, WordstatView.TOP_POPULAR)

    assert dataset.headers == [
        "Запросы со словами",
        "Число запросов",
        "Топ частотных запросов «новогодние подарки», 20.07.2026 — 20.08.2026, Россия, все устройства",
    ]
    assert all(";" not in header for header in dataset.headers)
    assert dataset.rows == []


def test_parse_wordstat_csv_reads_bom_and_cr_header_only_export(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    source.write_bytes(
        "Запросы со словами;Число запросов;Топ частотных запросов «новогодние подарки», Россия".encode(
            "utf-8-sig"
        )
        + b"\r"
    )

    dataset = parse_wordstat_csv(source, WordstatView.TOP_RELATED)

    assert dataset.headers == [
        "Запросы со словами",
        "Число запросов",
        "Топ частотных запросов «новогодние подарки», Россия",
    ]
    assert dataset.rows == []


def test_validate_headers_rejects_unparsed_semicolon(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"

    with pytest.raises(CsvFormatError, match="unparsed delimiter"):
        _validate_headers(["first;broken header"], source)


def test_parse_wordstat_csv_keeps_normal_multiline_export_working(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    source.write_text("Запрос;Число запросов\nчай;5\nкофе;3\n", encoding="utf-8")

    dataset = parse_wordstat_csv(source, WordstatView.TOP_POPULAR)

    assert dataset.headers == ["Запрос", "Число запросов"]
    assert dataset.rows == [{"Запрос": "чай", "Число запросов": "5"}, {"Запрос": "кофе", "Число запросов": "3"}]


def test_parse_wordstat_csv_rejects_duplicate_headers(tmp_path: Path) -> None:
    source = tmp_path / "report.csv"
    source.write_text("query,query\none,two\n", encoding="utf-8")

    with pytest.raises(CsvFormatError, match="duplicate headers"):
        parse_wordstat_csv(source, WordstatView.TOP_POPULAR)
