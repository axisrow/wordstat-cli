from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_test_hooks():
    path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("wordstat_test_hooks", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Reporter:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_sep(self, sep: str, title: str) -> None:
        self.lines.append(title)

    def write_line(self, message: str) -> None:
        self.lines.append(message)


def _run_guard(monkeypatch: pytest.MonkeyPatch, elapsed: float, *, disabled: bool = False):
    hooks = _load_test_hooks()
    reporter = _Reporter()
    session = SimpleNamespace(
        config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter)),
        exitstatus=pytest.ExitCode.OK,
    )
    ticks = iter((100.0, 100.0 + elapsed))
    monkeypatch.setattr(hooks.time, "monotonic", lambda: next(ticks))
    if disabled:
        monkeypatch.setenv(hooks.DISABLE_TEST_RUN_TIME_LIMIT_ENV, "1")

    hooks.pytest_sessionstart(session)
    hooks.pytest_sessionfinish(session, session.exitstatus)
    return session, reporter


def test_test_run_time_limit_fails_and_reports_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    session, reporter = _run_guard(monkeypatch, 21.25)

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert "test run time limit exceeded" in reporter.lines
    assert "21.25s" in reporter.lines[-1]
    assert "20s limit" in reporter.lines[-1]
    assert "network or browser" in reporter.lines[-1]
    assert "Optimize the tests" in reporter.lines[-1]


def test_test_run_time_limit_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    session, reporter = _run_guard(monkeypatch, 21.25, disabled=True)

    assert session.exitstatus == pytest.ExitCode.OK
    assert reporter.lines == []


def test_test_run_time_limit_allows_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    session, reporter = _run_guard(monkeypatch, 20.0)

    assert session.exitstatus == pytest.ExitCode.OK
    assert reporter.lines == []
