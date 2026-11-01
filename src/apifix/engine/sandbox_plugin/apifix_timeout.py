"""pytest plugin loaded by sandbox_driver.py (runs inside the container, Python 3.8+): gives
each test phase (setup, call, teardown) a time limit, so one hanging test fails by itself
instead of using up the whole test phase.

APIFIX_TEST_TIMEOUT: seconds per phase. APIFIX_TEST_BUDGET: seconds the whole session may
take; once it is used up the remaining tests fail at once. Either way the failure message
starts with MARKER, which the host reads from the JUnit report to name the timed-out tests.
"""

import os
import signal
import time

import pytest

MARKER = "apifix-timeout:"


class TestTimeout(BaseException):
    """Not an Exception, so a test's `except Exception` retry loop cannot swallow it."""

    __test__ = False


_state = {"started": time.monotonic()}


def _seconds(name):
    try:
        return float(os.environ.get(name, "") or 0)
    except ValueError:
        return 0.0


def pytest_sessionstart(session):
    _state["started"] = time.monotonic()


def _on_alarm(signum, frame):
    raise TestTimeout(_state["message"])


def _limit():
    """(seconds, why) for the next phase, or None for no limit."""
    per_test, budget = _seconds("APIFIX_TEST_TIMEOUT"), _seconds("APIFIX_TEST_BUDGET")
    left = budget - (time.monotonic() - _state["started"]) if budget else None
    if per_test and (left is None or per_test <= left):
        return per_test, f"{MARKER} test exceeded {per_test:g}s and was stopped"
    if left is not None:
        return max(left, 0), f"{MARKER} the sandbox's time budget ({budget:g}s) ran out"
    return None


def _guarded(item):
    limit = _limit() if hasattr(signal, "setitimer") else None
    if limit is None or item.config.pluginmanager.hasplugin("timeout"):  # pytest-timeout's own
        yield
        return
    seconds, _state["message"] = limit
    if seconds <= 0:  # budget already used up: fail this test at once
        raise TestTimeout(_state["message"])
    old = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    yield from _guarded(item)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    yield from _guarded(item)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_teardown(item):
    yield from _guarded(item)
