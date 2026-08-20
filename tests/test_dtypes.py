"""Column typing heuristics applied to Wordstat's localized string values."""

import pytest

from wordstat.dtypes import infer_column, parse_number


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5 228 679", 5228679),
        ("5 228 679", 5228679),
        ("5 228 679", 5228679),
        ("1 020", 1020),
        ("2026", 2026),
        ("-5", -5),
        ("12,5", 12.5),
        ("", None),
        ("   ", None),
    ],
)
def test_parse_number_accepts_wordstat_number_formats(value, expected):
    assert parse_number(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "01.2024",  # dynamics period, must never become 1.2024
        "1e5",
        "1_000",
        "inf",
        "nan",
        "3.1",  # Wordstat uses a comma as the decimal separator
        "12,5%",
        "2026-01",
        "н/д",
        "Москва",
    ],
)
def test_parse_number_rejects_non_numbers(value):
    assert parse_number(value) is NotImplemented


def test_dynamics_period_column_stays_string():
    assert infer_column(["01.2024", "02.2024", "03.2024"]) is None


def test_all_numeric_column_becomes_int64():
    assert infer_column(["5 228 679", "1 020", ""]) == ("int64", [5228679, 1020, None])


def test_fractional_value_promotes_whole_column_to_float64():
    dtype, values = infer_column(["12,5", "3", ""])
    assert dtype == "float64"
    assert values == [12.5, 3, None]


def test_single_non_numeric_value_keeps_whole_column_string():
    assert infer_column(["100", "200", "н/д"]) is None


def test_percentages_stay_string():
    assert infer_column(["12,5%", "3,1%"]) is None


def test_empty_cell_becomes_null_not_zero():
    _, values = infer_column(["100", ""])
    assert values == [100, None]


def test_fully_empty_column_stays_string():
    assert infer_column(["", "   ", ""]) is None
