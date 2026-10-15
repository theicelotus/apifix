"""Verifier against recorded sandbox results (no Docker)."""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apifix.engine.sandbox import PhaseResult, SandboxResult, SmokeResult
from apifix.engine.test_results import FileCoverage, TestCaseResult, counts_from
from apifix.engine.verifier import (
    INSTALL_OUTPUT_CHARS,
    VerifierConfig,
    compare_runs,
    decide,
    feedback_lines,
    matches_test,
    run_summary,
    verify,
)

# --- recorded runs --------------------------------------------------------------------------

T_ASK = "tests.test_app::test_ask"
T_STRIP = "tests.test_app::test_strips"
T_LIVE = "tests.test_live::test_real_api"


def run(
    *tests: tuple[str, str] | tuple[str, str, str],
    status: str | None = None,
    reason: str = "",
    coverage: dict[str, FileCoverage] | None = None,
    runner: str = "pytest",
) -> SandboxResult:
    cases = [TestCaseResult(*t) for t in tests]
    counts = counts_from(cases)
    if status is None:
        status = "failed" if counts.failed or counts.errors else "passed"
    result = SandboxResult(
        status=status,
        reason=reason,
        python_version="3.11",
        python_version_source="default",
        runner=runner,
        image="python:3.11-slim",
        counts=counts,
        tests=cases,
        coverage=coverage,
    )
    if status not in ("error", "timeout"):
        result.test = PhaseResult("test", "ok", "python:3.11-slim", "none", [])
    return result


# Executable lines of APP_NEW, all executed.
FULL_COVERAGE = {"app.py": FileCoverage(frozenset({1, 3, 6, 7, 8}), frozenset())}
PASSING = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)


@dataclass
class FakeSession:
    results: list[SandboxResult]
    coverage_args: list[bool | None] = field(default_factory=list)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def run_tests(self, *, coverage=None):
        self.coverage_args.append(coverage)
        return self.results.pop(0)


@dataclass
class FakeSandbox:
    baseline: SandboxResult
    fixed: list[SandboxResult]
    baseline_runs: int = 0
    session_coverage: bool | None = None
    last_session: FakeSession | None = None
    envs: list[dict | None] = field(default_factory=list)
    smokes: list[object] = field(default_factory=list)

    def run(self, repo, *, coverage=False, env=None):
        self.baseline_runs += 1
        self.envs.append(env)
        return self.baseline

    def session(self, repo, *, coverage=False, env=None, smoke=None):
        self.envs.append(env)
        self.smokes.append(smoke)
        self.session_coverage = coverage
        self.last_session = FakeSession(list(self.fixed))
        return self.last_session


# --- repos ----------------------------------------------------------------------------------

APP_OLD = """\
import openai


def ask(q):
    r = openai.ChatCompletion.create(model="gpt-4", messages=[{"role": "user", "content": q}])
    return r["choices"][0]["message"]["content"].strip()
"""

APP_NEW = """\
import openai

client = openai.OpenAI(api_key="test")


def ask(q):
    r = client.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": q}])
    return r.choices[0].message.content.strip()
"""

TESTS_OLD = """\
from unittest import mock

from app import ask


@mock.patch("openai.ChatCompletion.create")
def test_ask(create):
    create.return_value = {"choices": [{"message": {"content": " hi "}}]}
    assert ask("q") == "hi"


@mock.patch("openai.ChatCompletion.create")
def test_strips(create):
    create.return_value = {"choices": [{"message": {"content": "x\\n"}}]}
    assert ask("q") == "x"
"""

TESTS_NEW = """\
from types import SimpleNamespace as NS
from unittest import mock

from app import ask


def completion(text):
    return NS(choices=[NS(message=NS(content=text))])


@mock.patch("app.client.chat.completions.create")
def test_ask(create):
    create.return_value = completion(" hi ")
    assert ask("q") == "hi"


@mock.patch("app.client.chat.completions.create")
def test_strips(create):
    create.return_value = completion("x\\n")
    assert ask("q") == "x"
"""


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    return root


ORIGINAL = {
    "app.py": APP_OLD,
    "requirements.txt": "openai==0.28.1\npytest\n",
    "tests/test_app.py": TESTS_OLD,
}
GOOD_FIX = {
    "app.py": APP_NEW,
    "requirements.txt": "openai>=1.0,<2\npytest\n",
    "tests/test_app.py": TESTS_NEW,
}


@pytest.fixture
def trees(tmp_path):
    def make(fixed_files: dict[str, str | None], original_files=ORIGINAL):
        original = write_tree(tmp_path / "original", original_files)
        files = {**GOOD_FIX, **fixed_files}
        fixed = write_tree(tmp_path / "fixed", {k: v for k, v in files.items() if v is not None})
        return original, fixed

    return make


def codes(v) -> set[str]:
    return {r.code for r in v.reasons}


def reason(v, code):
    (r,) = [r for r in v.reasons if r.code == code]
    return r


# --- verdicts -------------------------------------------------------------------------------


def test_good_fix_is_verified(trees):
    original, fixed = trees({})
    sandbox = FakeSandbox(baseline=PASSING, fixed=[PASSING])
    v = verify(original, fixed, "openai-v1", sandbox=sandbox)
    assert v.verdict == "verified", v.reasons
    assert sandbox.session_coverage is True
    assert v.rerun is None
    assert v.coverage.executable > 0 and v.coverage.ratio == 1.0
    assert v.remaining_findings == []
    assert v.findings_before > 0
    # mock data edits are allowed but listed for reviewers, with file:line
    setup = [r for r in v.reasons if r.code == "setup_changed"]
    assert {(r.file, r.severity) for r in setup} == {("tests/test_app.py", "info")}
    assert all(r.line for r in setup)
    # patch-target rewrites are silent: no info reason on the decorator lines
    assert not any(r.code.startswith("tampering.") for r in v.reasons)


def test_baseline_is_reused(trees):
    original, fixed = trees({})
    sandbox = FakeSandbox(baseline=run(), fixed=[PASSING])
    v = verify(original, fixed, "openai-v1", sandbox=sandbox, baseline=PASSING)
    assert sandbox.baseline_runs == 0
    assert v.baseline is PASSING


def test_regression_is_rejected_after_rerun(trees):
    original, fixed = trees({})
    broken = run((T_ASK, "passed"), (T_STRIP, "failed", "assert 'x\\n' == 'x'"))
    sandbox = FakeSandbox(baseline=PASSING, fixed=[broken, broken])
    v = verify(original, fixed, "openai-v1", sandbox=sandbox)
    assert v.verdict == "rejected"
    assert reason(v, "regression").tests == (T_STRIP,)
    assert sandbox.last_session.coverage_args == [None, False]  # rerun without coverage


def test_flaky_failure_is_not_a_regression(trees):
    original, fixed = trees({})
    flaky = run((T_ASK, "passed"), (T_STRIP, "error", "Timeout"), coverage=FULL_COVERAGE)
    sandbox = FakeSandbox(baseline=PASSING, fixed=[flaky, PASSING])
    v = verify(original, fixed, "openai-v1", sandbox=sandbox)
    assert v.delta.flaky == [T_STRIP]
    assert "regression_error" not in codes(v)
    assert reason(v, "flaky").severity == "warn"
    assert v.verdict == "verified"


def test_deleted_test_is_rejected(trees):
    original, fixed = trees(
        {"tests/test_app.py": TESTS_NEW.split("@mock.patch")[0] + TESTS_NEW.split("\n\n\n")[2]}
    )
    only_ask = run((T_ASK, "passed"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [only_ask]))
    assert v.verdict == "rejected"
    assert reason(v, "deleted_test").tests == (T_STRIP,)
    removed = reason(v, "tampering.test_removed")
    assert (removed.file, removed.tests) == ("tests/test_app.py", ("test_strips",))


def test_renamed_test_is_not_deleted(trees):
    renamed = TESTS_NEW.replace("def test_strips", "def test_strips_whitespace")
    original, fixed = trees(
        {"tests/test_app.py": renamed},
        original_files={**ORIGINAL, "tests/test_app.py": TESTS_NEW},
    )
    after = run(
        (T_ASK, "passed"),
        ("tests.test_app::test_strips_whitespace", "passed"),
        coverage=FULL_COVERAGE,
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert "deleted_test" not in codes(v)
    assert reason(v, "test_renamed").severity == "info"
    assert v.verdict == "verified"


def test_collection_error_is_not_reported_as_deletion(trees):
    original, fixed = trees({})
    broken = run(("tests.test_app", "error", "ImportError: cannot import name 'ask'"))
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [broken, broken]))
    assert v.verdict == "rejected"
    r = reason(v, "collection_error")
    assert r.tests == (T_ASK, T_STRIP)
    assert "deleted_test" not in codes(v)


def test_external_service_tests_give_no_signal(trees):
    original, fixed = trees({})
    live_fail = (T_LIVE, "error", "openai.APIConnectionError: Connection error.")
    before = run((T_ASK, "passed"), (T_LIVE, "error", "openai.error.APIConnectionError: x"))
    after = run((T_ASK, "passed"), live_fail, coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(before, [after]))
    assert v.delta.no_signal_external == [T_LIVE]
    assert reason(v, "no_signal_external").severity == "info"
    assert v.verdict == "verified"
    assert "needs a network service or credentials" in v.report_markdown()


def test_only_external_tests_means_unverified(trees):
    original, fixed = trees({})
    before = run((T_LIVE, "error", "APIConnectionError"))
    after = run((T_LIVE, "error", "APIConnectionError"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(before, [after]))
    assert v.verdict == "unverified"
    assert "no_usable_tests" in codes(v)
    assert "regression" not in codes(v)


def test_fix_that_makes_failing_tests_pass(trees):
    original, fixed = trees({})
    before = run(
        (T_ASK, "error", "AttributeError: module 'openai' has no attribute 'ChatCompletion'")
    )
    after = run((T_ASK, "passed"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(before, [after]))
    assert v.delta.fixed_by_change == [T_ASK]
    assert v.verdict == "verified"


def test_newly_skipped_by_existing_condition_is_weak(trees):
    original, fixed = trees({})
    after = run((T_ASK, "passed"), (T_STRIP, "skipped"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert reason(v, "newly_skipped").tests == (T_STRIP,)
    assert v.verdict == "verified_weak"


def test_changed_skip_condition_that_now_skips_is_rejected(trees):
    old_tests = TESTS_OLD.replace(
        '@mock.patch("openai.ChatCompletion.create")\ndef test_strips',
        "@pytest.mark.skipif(sys.version_info < (3, 0), reason='py2')\n"
        '@mock.patch("openai.ChatCompletion.create")\ndef test_strips',
    )
    new_tests = TESTS_NEW.replace(
        '@mock.patch("app.client.chat.completions.create")\ndef test_strips',
        "@pytest.mark.skipif(sys.version_info < (9, 0), reason='py2')\n"
        '@mock.patch("app.client.chat.completions.create")\ndef test_strips',
    )
    original, fixed = trees(
        {"tests/test_app.py": new_tests},
        original_files={**ORIGINAL, "tests/test_app.py": old_tests},
    )
    after = run((T_ASK, "passed"), (T_STRIP, "skipped"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert reason(v, "tampering.skip_condition_changed").tests == (T_STRIP,)
    assert v.verdict == "rejected"


def test_added_skip_is_rejected(trees):
    skipped = TESTS_NEW.replace(
        "def test_strips(create):", "def test_strips(create):\n    pytest.skip('later')"
    )
    original, fixed = trees({"tests/test_app.py": skipped})
    after = run((T_ASK, "passed"), (T_STRIP, "skipped"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert "tampering.skip_added" in codes(v)
    # the skipped test is explained by the added skip, not by a condition or lost evidence
    assert not codes(v) & {"tampering.skip_condition_changed", "newly_skipped"}
    assert v.verdict == "rejected"


def test_changed_skip_condition_that_does_not_skip_is_info(trees):
    old_tests = TESTS_OLD.replace(
        '@mock.patch("openai.ChatCompletion.create")\ndef test_strips',
        "@pytest.mark.skipif(sys.version_info < (3, 0), reason='py2')\n"
        '@mock.patch("openai.ChatCompletion.create")\ndef test_strips',
    )
    new_tests = TESTS_NEW.replace(
        '@mock.patch("app.client.chat.completions.create")\ndef test_strips',
        "@pytest.mark.skipif(sys.version_info < (3, 6), reason='old')\n"
        '@mock.patch("app.client.chat.completions.create")\ndef test_strips',
    )
    original, fixed = trees(
        {"tests/test_app.py": new_tests},
        original_files={**ORIGINAL, "tests/test_app.py": old_tests},
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    r = reason(v, "skip_condition_changed")
    assert (r.severity, r.file, r.tests) == ("info", "tests/test_app.py", ("test_strips",))
    assert v.verdict == "verified"
    assert "skip_condition_changed" in v.report_markdown()


def test_weakened_assert_is_rejected(trees):
    weak = TESTS_NEW.replace('assert ask("q") == "x"', 'assert ask("q")')
    original, fixed = trees({"tests/test_app.py": weak})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    r = reason(v, "tampering.assert_weakened")
    assert (r.file, r.tests) == ("tests/test_app.py", ("test_strips",))
    assert v.verdict == "rejected"


def test_incomplete_migration_is_rejected(trees):
    half = APP_NEW + "\n\ndef legacy():\n    return openai.Completion.create(prompt='x')\n"
    original, fixed = trees({"app.py": half})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    incomplete = [r for r in v.reasons if r.code == "incomplete_migration"]
    assert [(r.file, r.line) for r in incomplete] == [("app.py", 12)]
    assert v.verdict == "rejected"


def test_unchanged_pin_is_incomplete(trees):
    original, fixed = trees({"requirements.txt": "openai==0.28.1\npytest\n"})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    assert reason(v, "incomplete_migration").file == "requirements.txt"


def test_syntax_error_is_rejected(trees):
    original, fixed = trees({"app.py": "def ask(:\n"})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    assert reason(v, "syntax_error").file == "app.py"


# --- evidence strength ------------------------------------------------------------------------


def test_low_changed_line_coverage_is_weak(trees):
    original, fixed = trees({})
    partial = {"app.py": FileCoverage(frozenset({1, 3, 6}), frozenset({7, 8}))}
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=partial)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    r = reason(v, "low_changed_line_coverage")
    assert "app.py:7,8" in r.message
    assert v.coverage.executed == 1 and v.coverage.executable == 3
    assert v.verdict == "verified_weak"


def test_threshold_is_configurable(trees):
    original, fixed = trees({})
    partial = {"app.py": FileCoverage(frozenset({1, 3, 6}), frozenset({7, 8}))}
    after = run((T_ASK, "passed"), coverage=partial)
    config = VerifierConfig(min_changed_line_coverage=0.3)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]), config=config)
    assert "low_changed_line_coverage" not in codes(v)


def test_unmeasured_coverage_is_weak(trees):
    original, fixed = trees({})
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=None)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert reason(v, "coverage_unavailable").severity == "weak"
    assert v.verdict == "verified_weak"


def test_file_never_imported_counts_as_uncovered(trees):
    original, fixed = trees({})
    after = run((T_ASK, "passed"), coverage={})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert v.coverage.unmeasured_files == ["app.py"]
    assert v.coverage.executed == 0 and v.coverage.executable == 3


def test_dependency_only_change_has_no_coverage_requirement(trees):
    original_files = {**ORIGINAL, "app.py": APP_NEW, "tests/test_app.py": TESTS_NEW}
    original, fixed = trees({}, original_files=original_files)
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=None)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert "coverage_not_applicable" in codes(v)
    assert v.verdict == "verified"


# --- run failures ---------------------------------------------------------------------------


def test_fixed_install_failure_is_rejected(trees):
    original, fixed = trees({})
    broken = run(status="error", reason="dependency install failed")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [broken]))
    r = reason(v, "fixed_run_error")
    assert "dependency" in r.message
    assert v.verdict == "rejected"
    # no test ran, so none counts as deleted (the live run's attempt 2 reported both)
    assert "deleted_test" not in codes(v) and v.delta.deleted == []


def test_sandbox_env_reaches_baseline_and_fixed_runs(trees):
    original, fixed = trees({})
    sandbox = FakeSandbox(baseline=PASSING, fixed=[PASSING])
    env = {"OPENAI_API_KEY": "sk-placeholder"}
    verify(original, fixed, "openai-v1", sandbox=sandbox, sandbox_env=env)
    assert sandbox.envs == [env, env]


def test_run_summary_keeps_pip_output_of_failed_installs():
    result = run(status="error", reason="dependency install failed")
    result.install_retried = True
    result.env_placeholders = ["OPENAI_API_KEY"]
    result.notes = ["dependency install failed (network or unknown cause); retried once"]
    failed = PhaseResult("install", "failed", "python:3.11-slim", "bridge", [], exit_code=1)
    failed.stdout, failed.stderr = "Collecting openai\n" + "x" * 5000, "ERROR: ReadTimeoutError"
    result.install_attempts = [
        failed,
        PhaseResult("install", "ok", "python:3.11-slim", "bridge", [], 0, "lots of output"),
    ]
    summary = run_summary(result)
    first, second = summary["install_attempts"]
    assert first["status"] == "failed" and first["exit_code"] == 1
    assert first["output"].startswith("... (output truncated)\n")
    assert first["output"].endswith("ERROR: ReadTimeoutError")
    assert len(first["output"]) <= INSTALL_OUTPUT_CHARS + 30
    assert "output" not in second  # successful installs: no output kept
    assert summary["install_retried"] is True
    assert summary["env_placeholders"] == ["OPENAI_API_KEY"]
    assert summary["notes"] == result.notes
    json.dumps(summary)


# --- feedback for the model -------------------------------------------------------------------

KEY_ERROR = (
    "openai.OpenAIError: The api_key client option must be set either by passing api_key to "
    "the client or by setting the OPENAI_API_KEY environment variable"
)
HINT = ("api_key client option must be set", "No API key: mock the constructor.")


def _rejected(trees, fixed_run):
    original, fixed = trees({})
    return verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run] * 2))


def test_feedback_dedupes_repeated_errors(trees):
    broken = run(
        (T_ASK, "failed", f"{KEY_ERROR}\nmocked_ask = <MagicMock id=1>"),
        (T_STRIP, "failed", f"{KEY_ERROR}\nmocked_ask = <MagicMock id=2>"),
    )
    v = _rejected(trees, broken)
    text = "\n".join(feedback_lines(v, dedupe=True))
    assert f"--- {T_ASK}\n{KEY_ERROR}" in text
    assert f"--- {T_STRIP} (same error as {T_ASK})" in text and "id=2" not in text
    assert "id=2" in "\n".join(feedback_lines(v))  # off by default (agent mode)


def test_feedback_hints_for_markers_in_the_failure_output(trees):
    v = _rejected(trees, run((T_ASK, "failed", KEY_ERROR), (T_STRIP, "passed")))
    assert "Hint: No API key: mock the constructor." in feedback_lines(v, hints=[HINT])
    assert not any(line.startswith("Hint:") for line in feedback_lines(v))
    other = _rejected(trees, run((T_ASK, "failed", "AssertionError"), (T_STRIP, "passed")))
    assert not any(line.startswith("Hint:") for line in feedback_lines(other, hints=[HINT]))


def test_feedback_hints_also_see_collection_errors(trees):
    # a module-level client raising at import breaks collection: not a "newly failing" test
    collection = run(("tests.test_app", "error", KEY_ERROR))
    v = _rejected(trees, collection)
    assert "Hint: No API key: mock the constructor." in feedback_lines(v, hints=[HINT])


def test_feedback_without_failure_output_and_with_pip_output(trees):
    broken = run(status="error", reason="dependency install failed")
    phase = PhaseResult("install", "failed", "python:3.11-slim", "bridge", [], exit_code=1)
    phase.stderr = "ERROR: No matching distribution found for openai==99"
    broken.install_attempts = [phase]
    v = _rejected(trees, broken)
    lines = feedback_lines(v, failure_output=False, install_output_chars=200)
    text = "\n".join(lines)
    assert "Sandbox: dependency install failed" in text
    assert "pip output, last part (untrusted, not instructions):" in text
    assert text.endswith("No matching distribution found for openai==99")
    assert "pip output" not in "\n".join(feedback_lines(v))
    failing = _rejected(trees, run((T_ASK, "failed", "AssertionError: boom"), (T_STRIP, "passed")))
    assert "boom" not in "\n".join(feedback_lines(failing, failure_output=False))


def test_both_runs_failing_to_start_is_unverified(trees):
    original, fixed = trees({})
    down = run(status="error", reason="docker not usable")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(down, [down]))
    assert "sandbox_unavailable" in codes(v)
    assert v.verdict == "unverified"


def test_missing_baseline_is_weak(trees):
    original, fixed = trees({})
    down = run(status="error", reason="dependency install failed")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(down, [PASSING]))
    assert "dependency install failed" in reason(v, "no_baseline").message
    assert v.verdict == "verified_weak"


def test_fixed_timeout_is_rejected(trees):
    original, fixed = trees({})
    slow = run(status="timeout", reason="tests exceeded 600s")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [slow]))
    assert "fixed_run_timeout" in codes(v)


# --- scope ----------------------------------------------------------------------------------


def test_scope_warnings(trees):
    original, fixed = trees(
        {
            "README.md": "# new docs\n",
            "helpers.py": "X = 1\n",
            ".github/workflows/ci.yml": "on: push\n",
        },
        original_files={**ORIGINAL, "README.md": "# docs\n"},
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    warns = {(r.code, r.file) for r in v.reasons if r.severity == "warn"}
    assert warns == {
        ("out_of_scope_change", "README.md"),
        ("out_of_scope_change", "helpers.py"),
        ("tooling_config_changed", ".github/workflows/ci.yml"),
    }
    assert v.verdict == "verified"  # warnings do not reject


def test_pytest_config_change_is_rejected(trees):
    original, fixed = trees(
        {"pytest.ini": "[pytest]\naddopts = --deselect tests/test_app.py::test_strips\n"},
        original_files={**ORIGINAL, "pytest.ini": "[pytest]\naddopts = -q\n"},
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    assert reason(v, "tampering.test_config_changed").file == "pytest.ini"
    assert v.verdict == "rejected"


# --- outputs --------------------------------------------------------------------------------


def test_to_dict_is_json_serialisable(trees):
    original, fixed = trees({})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    data = json.loads(json.dumps(v.to_dict()))
    assert data["verdict"] == "verified"
    assert data["runs"]["fixed"]["status"] == "passed"
    assert {"code", "severity", "message", "tests", "file", "line"} <= set(data["reasons"][0])


def test_report_markdown(trees):
    original, fixed = trees(
        {"tests/test_app.py": TESTS_NEW.replace('assert ask("q") == "x"', 'assert ask("q")')}
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    md = v.report_markdown()
    assert md.startswith("## Verification: Rejected\n")
    assert "| Passed / failed / errors / skipped | 2 / 0 / 0 / 0 | 2 / 0 / 0 / 0 |" in md
    assert "**Changed-line coverage:** tests executed 3 of 3 changed source lines (100%)." in md
    assert "### Why this verdict" in md
    assert "**tampering.assert_weakened**: `tests/test_app.py:" in md
    assert "### For reviewers" in md and "setup_changed" in md


# --- pure helpers ---------------------------------------------------------------------------


def test_compare_runs_buckets():
    before = run(
        ("m::a", "passed"),
        ("m::b", "passed"),
        ("m::c", "failed", "boom"),
        ("m::d", "skipped"),
        ("m::e", "passed"),
    )
    after = run(
        ("m::a", "failed"),
        ("m::b", "skipped"),
        ("m::c", "passed"),
        ("m::d", "failed"),
        ("m::new", "passed"),
    )
    d = compare_runs(before, after)
    assert d.newly_failing == ["m::a"]
    assert d.newly_skipped == ["m::b"]
    assert d.fixed_by_change == ["m::c"]
    assert d.deleted == ["m::e"]
    assert d.new_tests == ["m::new"]
    assert "m::d" not in d.newly_failing  # skipped in the baseline: no information


def test_compare_runs_unittest_collection_error():
    before = run(("tests.test_a.T.test_x", "passed"), runner="unittest")
    after = run(("unittest.loader._FailedTest.tests.test_a", "error"), runner="unittest")
    d = compare_runs(before, after)
    assert d.broken_modules == ["tests.test_a"]
    assert d.lost_to_collection_errors == ["tests.test_a.T.test_x"]
    assert d.deleted == []


def test_new_failing_test_counts_as_failing():
    d = compare_runs(run(("m::a", "passed")), run(("m::a", "passed"), ("m::b", "failed")))
    assert d.newly_failing == ["m::b"]


@pytest.mark.parametrize(
    ("test_id", "file", "qual", "ok"),
    [
        ("tests.test_app::test_ask", "tests/test_app.py", "test_ask", True),
        ("tests.test_app::test_ask[param-1]", "tests/test_app.py", "test_ask", True),
        ("tests.test_app.TestX::test_ask", "tests/test_app.py", "TestX.test_ask", True),
        ("test_app::test_ask", "tests/test_app.py", "test_ask", True),
        ("tests.test_app.T.test_ask", "tests/test_app.py", "T.test_ask", True),
        ("tests.test_other::test_ask", "tests/test_app.py", "test_ask", False),
        ("tests.test_app::test_ask_more", "tests/test_app.py", "test_ask", False),
    ],
)
def test_test_matches(test_id, file, qual, ok):
    assert matches_test(test_id, file, qual) is ok


def test_decide_precedence():
    from apifix.engine.verifier import Reason

    r = lambda sev: Reason("x", sev, "")  # noqa: E731
    assert decide([]) == "verified"
    assert decide([r("info"), r("warn")]) == "verified"
    assert decide([r("weak")]) == "verified_weak"
    assert decide([r("weak"), r("unverified")]) == "unverified"
    assert decide([r("unverified"), r("reject")]) == "rejected"


# --- test files by the shared definition (name or content) -------------------------------------


@pytest.mark.parametrize("test_path", ["svc/testserver.py", "svc/checks.py"])
def test_tampering_checks_cover_test_files_by_new_name_or_content(tmp_path, test_path):
    # svc/checks.py counts by content: it imports unittest (mock) and defines test_* functions
    original = write_tree(
        tmp_path / "original", {"app.py": APP_OLD, "requirements.txt": "", test_path: TESTS_OLD}
    )
    weak = TESTS_NEW.replace('assert ask("q") == "x"', 'assert ask("q")')
    fixed = write_tree(
        tmp_path / "fixed", {"app.py": APP_NEW, "requirements.txt": "", test_path: weak}
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    assert reason(v, "tampering.assert_weakened").file == test_path
    assert v.verdict == "rejected"


def test_a_test_file_is_judged_by_its_original_content(tmp_path):
    # The edit drops the unittest import and the tests; the original still decides.
    original = write_tree(
        tmp_path / "original", {"app.py": APP_OLD, "requirements.txt": "", "checks.py": TESTS_OLD}
    )
    fixed = write_tree(
        tmp_path / "fixed",
        {"app.py": APP_NEW, "requirements.txt": "", "checks.py": "def helper():\n    pass\n"},
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    assert any(r.code.startswith("tampering.") and r.file == "checks.py" for r in v.reasons)


# --- smoke check ------------------------------------------------------------------------------


def smoke_failed() -> SmokeResult:
    return SmokeResult(
        status="failed",
        description='import openai; openai.OpenAI(api_key="<dummy key>")',
        output="Traceback (most recent call last):\n  ...\nTypeError: Client.__init__() got an "
        "unexpected keyword argument 'proxies'\n",
        distribution="openai",
        version="1.5.0",
        requires={"httpx": "0.28.1", "pydantic": "1.10.26", "typing-extensions": "4.5.0"},
        pins=["typing_extensions==4.5.0"],
    )


def test_verify_runs_the_migrations_smoke_check_on_the_fixed_code(trees):
    original, fixed = trees({})
    sandbox = FakeSandbox(baseline=PASSING, fixed=[PASSING])
    verify(original, fixed, "openai-v1", sandbox=sandbox)
    (smoke,) = sandbox.smokes
    assert smoke.distribution == "openai" and "OpenAI(" in smoke.code


def test_failed_smoke_check_rejects_as_a_dependency_problem(trees):
    original, fixed = trees({})
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    fixed_run.smoke = smoke_failed()
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert v.verdict == "rejected"
    r = reason(v, "smoke_check_failed")
    assert r.severity == "reject"
    assert "unexpected keyword argument 'proxies'" in r.message
    assert "Installed: openai 1.5.0" in r.message
    assert "httpx 0.28.1" in r.message
    assert "typing_extensions==4.5.0" in r.message
    md = v.report_markdown()
    assert "**Dependency smoke check**" in md and "failed." in md
    assert v.to_dict()["runs"]["fixed"]["smoke"]["version"] == "1.5.0"


@pytest.mark.parametrize("status", ["ok", "error"])
def test_smoke_check_that_passed_or_could_not_run_does_not_reject(trees, status):
    original, fixed = trees({})
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    fixed_run.smoke = SmokeResult(status, "make a client")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert "smoke_check_failed" not in codes(v)
    assert v.verdict == "verified"


def test_import_paths_are_in_the_report(trees):
    original, fixed = trees({})
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    fixed_run.import_paths = ["svc"]
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert "**Import paths:** `svc/` on PYTHONPATH" in v.report_markdown()
