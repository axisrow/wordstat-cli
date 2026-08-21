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


def test_daily_period_rejects_a_start_date_further_back_than_the_trailing_60_day_window():
    # Live CDP measurement (issue #6 phase 2): the daily date-range
    # picker's own year-select popup only ever offers the current year —
    # a request for a valid-length window that starts far in the past
    # (e.g. 2019) is accepted by validate_period today, reaches the live
    # UI, and fails deep inside calendar-click code with an
    # InterfaceChangedError instead of being rejected up front. issue #6
    # step 3 explicitly asks for "daily granularity outside the 60-day
    # window" to be rejected before opening Chrome, which this closes.
    #
    # The bound is expressed relative to `today`, not the picker's year
    # list (a year-based rule would wrongly reject legal windows early in
    # January) and shares the existing 60-calendar-day convention: PR #20's
    # live-confirmed window 22.06.2026-20.08.2026, measured on 21.08.2026,
    # is exactly 60 days and must remain accepted.
    validate_period(Granularity.DAILY, date(2026, 6, 22), date(2026, 8, 20), today=date(2026, 8, 21))
    with pytest.raises(InvalidPeriodError, match="60 days in the past"):
        validate_period(Granularity.DAILY, date(2019, 1, 1), date(2019, 2, 15), today=date(2026, 8, 21))
    with pytest.raises(InvalidPeriodError, match="60 days in the past"):
        validate_period(Granularity.DAILY, date(2026, 6, 21), date(2026, 6, 25), today=date(2026, 8, 21))


def test_weekly_and_monthly_minimums():
    with pytest.raises(InvalidPeriodError, match="three calendar weeks"):
        validate_period(Granularity.WEEKLY, date(2026, 8, 1), date(2026, 8, 20))
    validate_period(Granularity.WEEKLY, date(2026, 8, 1), date(2026, 8, 21))
    with pytest.raises(InvalidPeriodError, match="three calendar months"):
        validate_period(Granularity.MONTHLY, date(2026, 8, 1), date(2026, 10, 1))
    validate_period(Granularity.MONTHLY, date(2026, 8, 1), date(2026, 10, 31))


def test_five_year_limit_is_not_assumed():
    validate_period(Granularity.MONTHLY, date(2018, 1, 1), date(2026, 8, 21))
