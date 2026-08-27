"""Project-wide pytest safeguards."""

from __future__ import annotations

import os
import sys
import time

import pytest

# Keep this threshold in one obvious place: the test suite is intentionally
# limited to fast, local unit tests.
TEST_RUN_TIME_LIMIT_SECONDS = 20.0
DISABLE_TEST_RUN_TIME_LIMIT_ENV = "WORDSTAT_DISABLE_TEST_TIME_LIMIT"


def _time_limit_is_disabled() -> bool:
    value = os.environ.get(DISABLE_TEST_RUN_TIME_LIMIT_ENV, "")
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def pytest_sessionstart(session: pytest.Session) -> None:
    setattr(session, "_wordstat_test_run_started_at", time.monotonic())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    started_at = getattr(session, "_wordstat_test_run_started_at", None)
    if started_at is None or _time_limit_is_disabled():
        return

    elapsed = time.monotonic() - started_at
    if elapsed <= TEST_RUN_TIME_LIMIT_SECONDS:
        return

    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    message = (
        f"pytest took {elapsed:.2f}s, exceeding the {TEST_RUN_TIME_LIMIT_SECONDS:g}s limit. "
        "Tests in this project intentionally do not access the network or browser; "
        "a slow run indicates a regression, not suite growth. Optimize the tests before continuing. "
        f"Set {DISABLE_TEST_RUN_TIME_LIMIT_ENV}=1 to disable this guard while debugging."
    )
    terminal_reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal_reporter is not None:
        terminal_reporter.write_sep("!", "test run time limit exceeded")
        terminal_reporter.write_line(message)
    else:
        print(message, file=sys.stderr)
