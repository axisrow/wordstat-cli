from datetime import date

import pytest

from wordstat.errors import InvalidPeriodError
from wordstat.periods import Granularity, parse_date, validate_period


def test_parse_date_uses_iso_calendar_dates():
    assert parse_date("2026-08-21") == date(2026, 8, 21)


@pytest.mark.parametrize("value", ["2026-02-30", "21.08.2026", "2026-13-01"])
def test_parse_date_rejects_invalid_values(value):
    with pytest.raises(InvalidPeriodError):
        parse_date(value)


def test_period_requires_both_bounds():
    with pytest.raises(InvalidPeriodError, match="provided together"):
        validate_period(Granularity.DAILY, date(2026, 8, 1), None)


def test_period_rejects_reversed_and_pre_2018_ranges():
    with pytest.raises(InvalidPeriodError, match="earlier"):
        validate_period(Granularity.DAILY, date(2026, 8, 20), date(2026, 8, 1))
    with pytest.raises(InvalidPeriodError, match="January 2018"):
        validate_period(Granularity.MONTHLY, date(2017, 12, 1), date(2018, 3, 1))


def test_daily_period_is_at_most_60_days_and_not_future():
    with pytest.raises(InvalidPeriodError, match="60"):
        validate_period(Granularity.DAILY, date(2026, 6, 1), date(2026, 7, 31), today=date(2026, 8, 21))
    with pytest.raises(InvalidPeriodError, match="after today"):
        validate_period(Granularity.DAILY, date(2026, 8, 20), date(2026, 8, 22), today=date(2026, 8, 21))
    validate_period(Granularity.DAILY, date(2026, 7, 1), date(2026, 8, 21), today=date(2026, 8, 21))


def test_weekly_and_monthly_minimums():
    with pytest.raises(InvalidPeriodError, match="three calendar weeks"):
        validate_period(Granularity.WEEKLY, date(2026, 8, 1), date(2026, 8, 20))
    validate_period(Granularity.WEEKLY, date(2026, 8, 1), date(2026, 8, 21))
    with pytest.raises(InvalidPeriodError, match="three calendar months"):
        validate_period(Granularity.MONTHLY, date(2026, 8, 1), date(2026, 10, 1))
    validate_period(Granularity.MONTHLY, date(2026, 8, 1), date(2026, 10, 31))


def test_five_year_limit_is_not_assumed():
    validate_period(Granularity.MONTHLY, date(2018, 1, 1), date(2026, 8, 21))
