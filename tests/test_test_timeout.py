"""Per-test time limit in the sandbox: the pytest plugin, the driver's flags, and how a
timed-out test is read back and judged."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

from apifix.engine import sandbox_driver as driver
from apifix.engine.sandbox import PLUGIN_DIR, TIMEOUT_MARKER
from apifix.engine.test_results import parse_junit
from apifix.engine.verifier import compare_runs, run_summary
from tests.test_verifier import run

HANGS = """
import time

def test_ok():
    assert True

def test_hangs():
    while True:  # a client retrying against a dead network
        try:
            time.sleep(0.05)
        except Exception:
            pass

def test_hangs_too():
    time.sleep(60)

def test_after():
    assert True
"""


def run_pytest(tmp_path: Path, source: str, *, timeout: str, budget: str = "0", extra=()):
    (tmp_path / "test_slow.py").write_text(textwrap.dedent(source))
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(PLUGIN_DIR),
        "APIFIX_TEST_TIMEOUT": timeout,
        "APIFIX_TEST_BUDGET": budget,
    }
    cmd = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "apifix_timeout"]
    cmd += [f"--junitxml={tmp_path}/junit.xml", *extra, str(tmp_path / "test_slow.py")]
    proc = subprocess.run(cmd, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    return proc, (tmp_path / "junit.xml").read_text()


def test_hanging_test_fails_alone_and_is_named(tmp_path):
    proc, xml = run_pytest(tmp_path, HANGS, timeout="1")
    counts, tests = parse_junit(xml)
    outcome = {t.id.split("::")[-1]: t.outcome for t in tests}
    assert outcome == {
        "test_ok": "passed",
        "test_hangs": "failed",
        "test_hangs_too": "failed",
        "test_after": "passed",
    }
    assert counts.passed == 2 and counts.failed == 2
    assert all(TIMEOUT_MARKER in t.message for t in tests if t.outcome == "failed")
    assert "exceeded 1s" in proc.stdout


def test_used_up_budget_fails_the_rest_at_once(tmp_path):
    source = """
    import time

    def test_first():
        time.sleep(60)

    def test_second():
        pass

    def test_third():
        pass
    """
    _, xml = run_pytest(tmp_path, source, timeout="30", budget="1")
    _, tests = parse_junit(xml)
    assert [t.outcome for t in tests] == ["failed", "error", "error"]  # the rest: setup errors
    assert all(TIMEOUT_MARKER in t.message and "budget" in t.message for t in tests)


def test_no_limit_leaves_tests_alone(tmp_path):
    source = "def test_a():\n    assert True\n"
    _, xml = run_pytest(tmp_path, source, timeout="0")
    counts, _ = parse_junit(xml)
    assert counts.passed == 1


def test_driver_adds_the_plugin_only_with_a_limit(monkeypatch, tmp_path):
    seen = {}

    def fake_call(command, env=None, cwd=None, **kw):
        seen["command"], seen["env"] = command, env
        return 0

    for name, value in (("WORK", tmp_path), ("RESULTS", tmp_path), ("DEPS", tmp_path)):
        monkeypatch.setattr(driver, name, str(value))
    monkeypatch.setattr(driver.subprocess, "check_call", lambda *a, **k: 0)
    monkeypatch.setattr(driver, "_site_dirs", lambda py: [str(tmp_path)])
    monkeypatch.setattr(driver.subprocess, "call", fake_call)
    command = ["{python}", "-m", "pytest", "-q"]

    driver.test(list(command), per_test_timeout=90, budget=500)
    assert seen["command"][1:6] == ["-m", "pytest", "-p", "apifix_timeout", "-q"]
    assert seen["env"]["APIFIX_TEST_TIMEOUT"] == "90"
    assert seen["env"]["APIFIX_TEST_BUDGET"] == "500"
    assert driver.PLUGINS in seen["env"]["PYTHONPATH"]

    driver.test(list(command))
    assert "apifix_timeout" not in seen["command"]
    assert "APIFIX_TEST_TIMEOUT" not in seen["env"]


def test_driver_parses_the_limit_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(driver, "test", lambda command, **kw: seen.update(kw) or 0)
    argv = ["test", "--per-test-timeout", "90", "--budget", "500", "--", "python", "-m", "pytest"]
    assert driver.main(argv) == 0
    assert (seen["per_test_timeout"], seen["budget"]) == (90, 500)
    assert driver.main(["test", "--budget", "soon", "--", "python"]) == 2


# --- judging: a test that times out on both sides is no signal ---------------------------

T = "tests.test_memory::test_learning"
MSG = f"{TIMEOUT_MARKER} test exceeded 120s and was stopped"


def test_timeout_on_both_sides_is_no_signal_not_a_regression():
    before = run((T, "failed", MSG), ("tests.t::ok", "passed"))
    after = run((T, "failed", MSG), ("tests.t::ok", "passed"))
    delta = compare_runs(before, after)
    assert delta.baseline_available
    assert delta.newly_failing == [] and delta.newly_erroring == []
    assert delta.no_signal_preexisting == [T]


def test_timeout_only_after_the_fix_is_a_regression():
    delta = compare_runs(run((T, "passed")), run((T, "failed", MSG)))
    assert delta.newly_failing == [T]


def test_timeout_only_before_the_fix_is_fixed_by_it():
    delta = compare_runs(run((T, "failed", MSG)), run((T, "passed")))
    assert delta.fixed_by_change == [T]


def test_run_summary_names_timed_out_tests():
    r = run((T, "failed", MSG))
    assert "timed_out_tests" not in run_summary(r)
    r.timed_out_tests = [T]
    assert run_summary(r)["timed_out_tests"] == [T]
