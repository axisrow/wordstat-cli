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
    it as a reliable live fact.
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
    elif granularity is Granularity.WEEKLY:
        # Live CDP checks (issue #6 phase 2) confirmed Wordstat's weekly view
        # silently ignores an explicit --date-from/--date-to and returns its
        # default ~2-year window instead (107 full weeks, unrelated to the
        # requested dates). Returning that mismatched window with a manifest
        # claiming the requested period would be the worst outcome, so an
        # explicit weekly period is rejected outright rather than collected
        # and silently wrong. Weekly without an explicit period still works
        # (see docs/issue6-phase1-findings.md) and remains allowed below.
        raise InvalidPeriodError(
            "Weekly granularity with an explicit period is not supported: "
            "Wordstat ignores the requested window and returns its default "
            "range instead; use daily or monthly, or omit --date-from/--date-to"
        )
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
