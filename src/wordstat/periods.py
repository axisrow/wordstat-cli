"""Validation and normalization of user-requested dynamics periods."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from enum import StrEnum

from wordstat.errors import InvalidPeriodError


class Granularity(StrEnum):
    MONTHLY = "monthly"
    WEEKLY = "weekly"
    DAILY = "daily"


EARLIEST_DATE = date(2018, 1, 1)


def validate_period(
    granularity: Granularity,
    date_from: date | None,
    date_to: date | None,
    *,
    today: date | None = None,
) -> None:
    """Validate an explicit window before opening Chrome.

    Omitted dates leave the UI's default window untouched. The five-year
    maximum is deliberately not enforced because phase 1 did not establish
    it as a reliable live fact — unlike the daily lower bound below, which
    is a confirmed live limitation of the picker itself, not a policy
    choice.
    """
    if (date_from is None) != (date_to is None):
        raise InvalidPeriodError("--date-from and --date-to must be provided together")
    if date_from is None or date_to is None:
        return
    if date_from < EARLIEST_DATE or date_to < EARLIEST_DATE:
        raise InvalidPeriodError("The requested period cannot include dates before January 2018")
    if date_to < date_from:
        raise InvalidPeriodError("The period end date must not be earlier than its start date")
    if granularity is Granularity.DAILY:
        current = today or date.today()
        if date_to > current:
            raise InvalidPeriodError("Daily statistics cannot be requested after today")
        if date_to - date_from >= timedelta(days=60):
            raise InvalidPeriodError("Daily statistics support at most 60 calendar days")
        # Live CDP measurement (issue #6 phase 2): the daily date-range
        # picker's year-select popup only ever offers the current year, so
        # a date_from further back than the trailing 60-day window cannot
        # actually be selected in the live UI at all — it currently reaches
        # Chrome and fails deep inside calendar-click code with an opaque
        # InterfaceChangedError instead of a clear pre-flight rejection.
        # Expressed relative to `today` (not "reject any year but the
        # current one" — that would wrongly reject legal windows in
        # January) and kept consistent with the existing 60-day window
        # check above: PR #20's live-confirmed window 22.06.2026-20.08.2026,
        # measured on 21.08.2026, is exactly 60 days back from today and
        # must remain accepted.
        if current - date_from > timedelta(days=60):
            raise InvalidPeriodError("Daily statistics cannot start more than 60 days in the past")
    elif granularity is Granularity.WEEKLY:
        if date_to - date_from < timedelta(days=20):
            raise InvalidPeriodError("Weekly statistics require at least three calendar weeks")
    else:
        end_month = date_from.month + 2
        end_year = date_from.year + (end_month - 1) // 12
        end_month = (end_month - 1) % 12 + 1
        minimum_end = date(end_year, end_month, monthrange(end_year, end_month)[1])
        if date_to < minimum_end:
            raise InvalidPeriodError("Monthly statistics require at least three calendar months")


def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        year, month, day = (int(part) for part in value.split("-"))
        if month < 1 or month > 12 or day < 1 or day > monthrange(year, month)[1]:
            raise ValueError
        return date(year, month, day)
    except (TypeError, ValueError):
        raise InvalidPeriodError(f"Invalid date {value!r}; expected YYYY-MM-DD") from None
