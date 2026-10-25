"""Verifier against recorded sandbox results (no Docker)."""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apifix.engine.migrations.openai_v1 import rules as openai_rules
from apifix.engine.models import LibraryTarget
from apifix.engine.project import NotInstalled
from apifix.engine.sandbox import PhaseResult, SandboxResult, SmokeResult
from apifix.engine.test_results import FileCoverage, TestCaseResult, TestCounts, counts_from
from apifix.engine.verifier import (
    INSTALL_OUTPUT_CHARS,
    MockFailure,
    TestDelta,
    VerifierConfig,
    _why_failing,
    compare_runs,
    decide,
    failure_hints,
    feedback_lines,
    matches_test,
    mock_failure,
    mock_failures,
    only_mock_failures,
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
    originals: list[object] = field(default_factory=list)
    targets: list[object] = field(default_factory=list)

    def run(self, repo, *, coverage=False, env=None):
        self.baseline_runs += 1
        self.envs.append(env)
        return self.baseline

    def session(self, repo, *, coverage=False, env=None, smoke=None, original=None, target=None):
        self.envs.append(env)
        self.smokes.append(smoke)
        self.originals.append(original)
        self.targets.append(target)
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
    "requirements.txt": "openai>=1.55.3,<3\npytest\n",
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


AUTOSPEC_ERROR = (
    "AttributeError: Mock object has no attribute 'completions'\n"
    "OpenAI = <MagicMock name='OpenAI' spec='OpenAI' id='1'>\n\n"
    '    @mock.patch("app.OpenAI", autospec=True)\n'
    "    def test_ask(OpenAI):\n"
    ">       OpenAI.return_value.chat.completions.create.return_value = reply\n"
    "E       AttributeError: Mock object has no attribute 'completions'"
)


def test_regressions_keep_why_they_fail_in_json_and_report(trees):
    # genai-gptoss-4: the verify outputs named the 5 regressions without their messages
    original, fixed = trees({})
    T_NEW = "tests.test_app::test_new"
    broken = run(
        (T_ASK, "failed", AUTOSPEC_ERROR),
        (T_STRIP, "error", "fixture 'mocker' not found"),
        (T_NEW, "failed", "assert 1 == 2"),
        coverage=FULL_COVERAGE,
    )
    sandbox = FakeSandbox(baseline=PASSING, fixed=[broken, broken])
    v = verify(original, fixed, "openai-v1", sandbox=sandbox)
    assert v.verdict == "rejected"
    lines = AUTOSPEC_ERROR.splitlines()
    assert v.delta.regression_messages == {
        T_ASK: "\n".join(lines[:2] + lines[3:6]),  # 5 lines: the blank one skipped
        T_STRIP: "fixture 'mocker' not found",
        T_NEW: "assert 1 == 2",  # a new test that fails
    }
    data = json.loads(json.dumps(v.to_dict()))
    assert data["tests"]["regression_messages"][T_STRIP] == "fixture 'mocker' not found"
    md = v.report_markdown()
    assert (
        "### Tests that now fail\n\n"
        f"- `{T_ASK}`: now fails: `AttributeError: Mock object has no attribute 'completions'`\n"
        f"- `{T_NEW}`: now fails: `assert 1 == 2`\n"
        f"- `{T_STRIP}`: now errors: `fixture 'mocker' not found`\n"
    ) in md
    assert md.index("### Why this verdict") < md.index("### Tests that now fail")


def test_flaky_tests_drop_their_regression_message(trees):
    original, fixed = trees({})
    flaky = run((T_ASK, "passed"), (T_STRIP, "failed", "Timeout"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [flaky, PASSING]))
    assert v.delta.flaky == [T_STRIP] and v.delta.regression_messages == {}
    assert "### Tests that now fail" not in v.report_markdown()


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


# genai-gptoss-6: the test built its client from SimpleNamespaces without `create`
NS_CREATE = "AttributeError: 'types.SimpleNamespace' object has no attribute 'create'"
MOCK_HINT = "the test's mock client is incomplete: {problem}; use MagicMock()"


@pytest.mark.parametrize(
    ("message", "problem", "spec"),
    [
        (
            f"{NS_CREATE}\nget_client = <MagicMock name='get_client'>",
            "SimpleNamespace has no create",
            False,
        ),
        ("TypeError: 'MagicMock' object is not iterable", "MagicMock is not iterable", False),
        (
            "TypeError: 'unittest.mock.Mock' object is not subscriptable",
            "Mock is not subscriptable",
            False,
        ),
        (
            "TypeError: object MagicMock can't be used in 'await' expression",
            "MagicMock can't be used in 'await' expression",
            False,
        ),
        (
            "AttributeError: Mock object has no attribute 'completions'",
            "Mock has no completions",
            True,
        ),
    ],
)
def test_mock_failures_are_recognised(message, problem, spec):
    assert mock_failure(T_ASK, message) == MockFailure(T_ASK, problem, spec)


@pytest.mark.parametrize(
    "message",
    [
        "AttributeError: 'Response' object has no attribute 'choices'",  # not a mock
        "AttributeError: module 'openai' has no attribute 'ChatCompletion'",
        "TypeError: 'dict' object is not callable",
        "AssertionError: assert 'x' == 'y'",
    ],
)
def test_other_errors_are_not_mock_failures(message):
    assert mock_failure(T_ASK, message) is None


def test_only_mock_failures_and_the_hint(trees):
    v = _rejected(trees, run((T_ASK, "failed", NS_CREATE), (T_STRIP, "error", NS_CREATE)))
    assert set(mock_failures(v)) == {T_ASK, T_STRIP} and only_mock_failures(v)
    hints = [line for line in feedback_lines(v, mock_hint=MOCK_HINT) if line.startswith("Hint")]
    # one hint per distinct problem
    assert hints == [
        "Hint: the test's mock client is incomplete: SimpleNamespace has no create; use MagicMock()"
    ]
    assert not any(line.startswith("Hint") for line in feedback_lines(v))
    # one test failing otherwise: the code under test may be at fault too
    mixed = _rejected(trees, run((T_ASK, "failed", NS_CREATE), (T_STRIP, "failed", "boom")))
    assert set(mock_failures(mixed)) == {T_ASK} and not only_mock_failures(mixed)
    # a spec'd mock (autospec) has its own hint
    spec = _rejected(
        trees,
        run(
            (T_ASK, "failed", "AttributeError: Mock object has no attribute 'completions'"),
            (T_STRIP, "passed"),
        ),
    )
    assert only_mock_failures(spec)
    assert failure_hints(spec, (), MOCK_HINT) == []
    # another blocking reason (a remaining finding) is not the tests' mocks
    original, fixed = trees({"app.py": APP_OLD})
    stale = verify(
        original,
        fixed,
        "openai-v1",
        sandbox=FakeSandbox(PASSING, [run((T_ASK, "failed", NS_CREATE), (T_STRIP, "passed"))] * 2),
    )
    assert "incomplete_migration" in codes(stale) and not only_mock_failures(stale)


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


CONFTEST_ERROR = (
    "ImportError while loading conftest 'tests/conftest.py': ValueError: numpy.dtype size "
    "changed, may indicate binary incompatibility"
)


def usage_error(message: str = CONFTEST_ERROR) -> SandboxResult:
    result = run(
        status="error", reason=f"pytest exited 4 (usage error) without a report: {message}"
    )
    result.test = PhaseResult("test", "failed", "python:3.11-slim", "none", [], exit_code=4)
    result.runner_error = message
    result.runner_output = f"...\nE   {message.split(': ', 1)[1]}"
    return result


def test_pytest_exit_4_on_both_versions_is_an_environment_problem(trees):
    original, fixed = trees({})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(usage_error(), [usage_error()]))
    assert v.verdict == "unverified"
    assert "pytest_usage_error" in codes(v) and "sandbox_unavailable" not in codes(v)
    reason = next(r for r in v.reasons if r.code == "pytest_usage_error")
    assert CONFTEST_ERROR in reason.message
    assert "stopped the same way" in reason.message
    assert "pytest_usage_error" in v.report_markdown()
    assert "pytest_usage_error" not in "\n".join(feedback_lines(v))  # environment: not sent
    summary = run_summary(v.baseline)
    assert summary["runner_error"] == CONFTEST_ERROR
    assert "numpy.dtype" in summary["runner_output"]


def test_pytest_exit_4_with_a_different_error_on_the_fix_names_both(trees):
    original, fixed = trees({})
    other = usage_error("pytest: error: unrecognized arguments: --cov=app")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(usage_error(), [other]))
    reason = next(r for r in v.reasons if r.code == "pytest_usage_error")
    assert CONFTEST_ERROR in reason.message and "--cov=app" in reason.message


def test_pytest_exit_4_on_the_fix_alone_is_a_rejection_with_its_error(trees):
    original, fixed = trees({})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [usage_error()]))
    assert v.verdict == "rejected"
    reason = next(r for r in v.reasons if r.code == "fixed_run_error")
    assert CONFTEST_ERROR in reason.message
    assert "pytest_usage_error" not in codes(v)


def test_missing_baseline_is_weak(trees):
    original, fixed = trees({})
    down = run(status="error", reason="dependency install failed")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(down, [PASSING]))
    assert "dependency install failed" in reason(v, "no_baseline").message
    assert v.verdict == "verified_weak"


# The resume-analyzer run: the baseline's install timed out, and four tests that need files the
# repo does not ship fail on every version.
INSTALL_TIMEOUT = run(status="timeout", reason="dependency install exceeded 900s")
NEEDS_FILES = run(
    (T_ASK, "passed"),
    (T_STRIP, "failed", "FileNotFoundError: [Errno 2] No such file: 'resumes/resume.pdf'"),
    ("tests.test_app::test_upload", "failed", "FileNotFoundError: 'resumes/x.pdf'"),
    coverage=FULL_COVERAGE,
)


def test_without_a_baseline_failing_tests_are_not_regressions(trees):
    original, fixed = trees({})
    sandbox = FakeSandbox(INSTALL_TIMEOUT, [NEEDS_FILES])
    v = verify(original, fixed, "openai-v1", sandbox=sandbox)
    assert v.verdict == "unverified", v.reasons
    assert not codes(v) & {"regression", "regression_error", "collection_error", "deleted_test"}
    assert v.delta.newly_failing == [] and v.delta.new_tests == []
    failing = ["tests.test_app::test_strips", "tests.test_app::test_upload"]
    assert v.delta.failing_without_baseline == failing
    r = reason(v, "unjudged_failures")
    assert r.severity == "unverified" and list(r.tests) == failing
    assert r.message.startswith("2 test(s) fail on the fixed code, but the original code's tests")
    assert "(dependency install exceeded 900s)" in r.message
    assert "not counted as regressions" in r.message
    assert "no_baseline" in codes(v)
    assert v.rerun is None and sandbox.last_session.results == []  # no flaky rerun either
    assert v.to_dict()["tests"]["failing_without_baseline"] == failing
    md = v.report_markdown()
    assert md.startswith("## Verification: Unverified (not enough evidence)\n")
    assert "### Failing tests (no baseline to compare with)" in md
    assert "- `tests.test_app::test_upload`" in md
    # the model is not shown these as regressions to fix
    assert "Failure output" not in "\n".join(feedback_lines(v))


def test_a_baseline_whose_test_phase_crashed_is_not_compared_either(trees):
    original, fixed = trees({})
    crashed = run(status="error", reason="pytest exited 2 without a report")
    crashed.test = PhaseResult("test", "failed", "python:3.11-slim", "none", [], exit_code=2)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(crashed, [NEEDS_FILES]))
    assert v.verdict == "unverified"
    assert "regression" not in codes(v) and "unjudged_failures" in codes(v)
    assert v.delta.baseline_available is False


def test_without_a_baseline_a_collection_error_is_not_a_rejection(trees):
    original, fixed = trees({})
    broken = run(("tests.test_app", "error", "ImportError: no module named x"), (T_ASK, "passed"))
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(INSTALL_TIMEOUT, [broken]))
    assert v.verdict == "unverified"
    assert v.delta.failing_without_baseline == ["tests.test_app"]
    assert v.delta.broken_modules == []


def test_without_a_baseline_other_problems_still_reject(trees):
    original, fixed = trees({"app.py": APP_OLD})  # call sites left unmigrated
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(INSTALL_TIMEOUT, [NEEDS_FILES]))
    assert v.verdict == "rejected"
    assert "incomplete_migration" in codes(v) and "regression" not in codes(v)


def test_no_test_files_in_the_original_is_a_usable_baseline():
    none = run(status="unverified", reason="no test files found")
    none.test = None
    d = compare_runs(none, run(("m::new", "failed")))
    assert d.baseline_available is True
    assert d.newly_failing == ["m::new"] and d.failing_without_baseline == []


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


def test_compare_runs_keeps_the_messages_of_no_signal_tests_only():
    before = run(
        ("m::pre", "failed", "KeyError: 'x'"),
        ("m::ext", "error", "openai.error.APIConnectionError: x"),
        ("m::fixed", "failed", "boom"),
        ("m::new_fail", "passed"),
        ("broken", "error", "collection failure\nImportError"),
    )
    after = run(
        ("m::pre", "error", "TypeError: y"),
        ("m::ext", "error", "openai.APIConnectionError: Connection error."),
        ("m::fixed", "passed"),
        ("m::new_fail", "failed", "nope"),
        ("broken", "error", "collection failure\nSyntaxError"),
    )
    d = compare_runs(before, after)
    assert d.no_signal_preexisting == ["m::pre", "broken"]
    assert d.no_signal_external == ["m::ext"]
    assert d.no_signal_messages == {
        "m::pre": {"baseline": "KeyError: 'x'", "fixed": "TypeError: y"},
        "m::ext": {
            "baseline": "openai.error.APIConnectionError: x",
            "fixed": "openai.APIConnectionError: Connection error.",
        },
        "broken": {
            "baseline": "collection failure\nImportError",
            "fixed": "collection failure\nSyntaxError",
        },
    }


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


OVERRIDE_LABEL = "--sandbox-requirements reqs.txt"


def held_back_run(pin_sources: dict[str, list[str]]) -> SandboxResult:
    """The fixed run when the installed openai is 0.x: every test module fails to import."""
    message = "ImportError: cannot import name 'OpenAI' from 'openai'"
    fixed_run = run(("tests.test_app", "error", message))
    fixed_run.requirements_override = OVERRIDE_LABEL
    fixed_run.smoke = SmokeResult(
        status="failed",
        description='import openai; openai.OpenAI(api_key="<dummy key>")',
        output="AttributeError: module 'openai' has no attribute 'OpenAI'\n",
        distribution="openai",
        version="0.28.1",
        requires={"typing-extensions": "4.5.0"},
        pins=sorted(pin_sources),
        pin_sources=pin_sources,
    )
    return fixed_run


def test_verify_passes_the_migrations_library_target_to_the_fixed_run(trees):
    original, fixed = trees({})
    sandbox = FakeSandbox(baseline=PASSING, fixed=[PASSING])
    verify(original, fixed, "openai-v1", sandbox=sandbox)
    assert sandbox.targets == [
        LibraryTarget("openai", "1.0", ">=1.55.3,<3", dict(openai_rules.COMPANION_PINS))
    ]


def test_smoke_failure_from_sandbox_requirements_pins_only_is_not_the_fixs_problem(trees):
    original, fixed = trees({})
    fixed_run = held_back_run({"typing_extensions==4.5.0": [OVERRIDE_LABEL]})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert "smoke_check_failed" not in codes(v)
    r = reason(v, "sandbox_requirements_pin")
    assert r.severity == "unverified"
    assert (
        "The only exact pins that can hold openai back to an old release come from "
        "--sandbox-requirements reqs.txt: typing_extensions==4.5.0" in r.message
    )
    assert "apifix verify ... --sandbox-requirements FILE" in r.message
    # the import errors follow from the wrong openai: not judged, not rejected
    for code in ("collection_error",):
        assert reason(v, code).severity == "unverified"
        assert "not judged: --sandbox-requirements held openai back" in reason(v, code).message
    assert v.verdict == "unverified"
    # an environment reason: never sent to the model
    assert "sandbox_requirements_pin" not in "\n".join(feedback_lines(v))
    assert "sandbox_requirements_pin" in v.report_markdown()


def test_sandbox_requirements_pin_leaves_independent_rejections(trees):
    original, fixed = trees({"tests/test_app.py": None})  # the fix deleted the tests
    fixed_run = held_back_run({"typing_extensions==4.5.0": [OVERRIDE_LABEL]})
    fixed_run.tests, fixed_run.counts = [], TestCounts()
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert "sandbox_requirements_pin" in codes(v)
    assert reason(v, "deleted_test").severity == "reject"
    assert v.verdict == "rejected"


@pytest.mark.parametrize(
    "pin_sources",
    [
        # one of the pins is the repo's own: the model can change that one
        {"openai==0.28.1": [OVERRIDE_LABEL], "typing_extensions==4.5.0": ["requirements.txt"]},
        {},  # no pins at all
    ],
)
def test_smoke_failure_with_repo_pins_or_none_stays_a_dependency_problem(trees, pin_sources):
    original, fixed = trees({})
    fixed_run = held_back_run(pin_sources)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert "sandbox_requirements_pin" not in codes(v)
    assert reason(v, "smoke_check_failed").severity == "reject"
    assert reason(v, "collection_error").severity == "reject"
    assert v.verdict == "rejected"


def test_smoke_failure_names_lockfile_pins_as_the_lockfiles(trees):
    original, fixed = trees({})
    fixed_run = held_back_run(
        {"tqdm==4.65.0": ["poetry.lock"], "typing-extensions==4.7.1": ["pyproject.toml"]}
    )
    fixed_run.requirements_override = None
    fixed_run.lockfile = "poetry.lock"
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    message = reason(v, "smoke_check_failed").message
    assert (
        "dependency files on openai or its dependencies, which can hold openai back to an "
        "old release: typing-extensions==4.7.1." in message
    )
    assert (
        "Exact pins from poetry.lock (apifix applies them as constraints and never edits "
        "it; regenerate it after the upgrade) on openai or its dependencies: tqdm==4.65.0."
        in message
    )


def test_report_says_when_the_fixed_run_got_the_target_for_the_users_pin(trees):
    original, fixed = trees({})
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    fixed_run.requirements_override = "--sandbox-requirements suspense-sandbox-reqs.txt"
    fixed_run.override_upgraded = ["openai==0.28.1 -> openai>=1.55.3,<3"]
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert (
        "Fixed run: `openai==0.28.1` from --sandbox-requirements suspense-sandbox-reqs.txt "
        "replaced by `openai>=1.55.3,<3` (the migration's target; no edit of the fix changed this "
        "library)." in v.report_markdown()
    )
    summary = v.to_dict()["runs"]["fixed"]
    assert summary["override_upgraded"] == ["openai==0.28.1 -> openai>=1.55.3,<3"]


def test_import_paths_are_in_the_report(trees):
    original, fixed = trees({})
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    fixed_run.import_paths = ["svc"]
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    assert "**Import paths:** `svc/` on PYTHONPATH" in v.report_markdown()


# --- environment ------------------------------------------------------------------------------


def no_env_baseline() -> SandboxResult:
    """The original code's tests all fail on modules the environment lacks."""
    baseline = run(
        ("tests.test_app", "error", "collection failure", "dotenv.main"),
        ("tests.test_spotify", "error", "collection failure", "spotipy"),
        ("tests.test_os", "error", "collection failure", "os.missing"),  # stdlib: not counted
        ("tests.test_local", "error", "collection failure", "app"),  # repo module: not counted
    )
    baseline.declared = {"openai": ["requirements.txt"]}
    baseline.not_installed = [
        NotInstalled(
            "spotipy",
            "pyproject.toml [tool.pixi.dependencies]",
            "conda-only package; not supported yet",
        ),
    ]
    return baseline


def test_environment_incomplete_names_missing_modules_and_where_they_are_declared(trees):
    original, fixed = trees({})
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(no_env_baseline(), [PASSING]))
    assert v.verdict == "unverified"
    r = reason(v, "environment_incomplete")
    assert r.severity == "unverified"
    assert "dotenv (python-dotenv, not declared in any dependency file apifix reads)" not in (
        r.message
    )
    assert "dotenv (not declared in any dependency file apifix reads)" in r.message
    assert (
        "spotipy (pyproject.toml [tool.pixi.dependencies]: not installed (conda-only package; "
        "not supported yet))" in r.message
    )
    assert "; os (" not in r.message and "; app (" not in r.message  # stdlib, repo module
    assert "--sandbox-requirements FILE" in r.message
    assert r.tests == ("tests.test_app", "tests.test_spotify")
    # the model never sees it
    lines = "\n".join(feedback_lines(v))
    assert "environment_incomplete" not in lines and "sandbox's environment" not in lines


def test_environment_detail_uses_the_import_name_table(trees):
    original, fixed = trees({})
    baseline = run(("tests.test_app", "error", "collection failure", "dotenv"))
    baseline.declared = {"python-dotenv": ["pyproject.toml"]}
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [PASSING]))
    assert (
        "dotenv (python-dotenv, declared in pyproject.toml, but not importable after the "
        "install)" in reason(v, "environment_incomplete").message
    )


def test_missing_modules_with_passing_baseline_tests_are_only_info(trees):
    original, fixed = trees({})
    baseline = run(
        (T_ASK, "passed"),
        (T_STRIP, "passed"),
        ("tests.test_extra", "error", "collection failure", "spotipy"),
    )
    fixed_run = run(
        (T_ASK, "passed"),
        (T_STRIP, "passed"),
        ("tests.test_extra", "error", "collection failure", "spotipy"),
        coverage=FULL_COVERAGE,
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [fixed_run]))
    assert "environment_incomplete" not in codes(v)
    r = reason(v, "missing_modules")
    assert r.severity == "info" and "spotipy (not declared" in r.message


HEAVY = {"tests/test_heavy.py": "import nlpaug\n\n\ndef test_augment():\n    pass\n"}
NO_NLPAUG = "ModuleNotFoundError: No module named 'nlpaug'"


def test_module_failing_to_collect_before_and_after_is_no_signal_with_its_file(trees):
    # SuspensePerception: one test file needs undeclared packages; the others still ran
    original, fixed = trees(HEAVY, {**ORIGINAL, **HEAVY})
    heavy = ("tests.test_heavy", "error", NO_NLPAUG, "nlpaug")
    baseline = run((T_ASK, "passed"), (T_STRIP, "passed"), heavy)
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), heavy, coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [fixed_run]))
    assert v.delta.no_signal_preexisting == ["tests.test_heavy"]
    assert v.delta.broken_modules == [] and v.delta.newly_erroring == []
    assert not codes(v) & {"collection_error", "regression_error", "environment_incomplete"}
    r = reason(v, "missing_modules")
    assert r.severity == "info"
    assert r.message.endswith(
        "nlpaug (not declared in any dependency file apifix reads). nlpaug blocks "
        "tests/test_heavy.py; the other tests ran"
    )
    assert reason(v, "no_signal_preexisting").tests == ("tests.test_heavy",)
    assert v.verdict == "verified"


IMPORT_ERROR = (
    "collection failure\nImportError while importing test module '/work/tests/test_heavy.py'.\n"
    "Traceback:\n...\nE   " + NO_NLPAUG
)


def test_no_signal_tests_keep_why_they_fail_in_json_and_report(trees):
    # SuspensePerception: 11 of 13 tests failed before and after, listed with names only
    original, fixed = trees(HEAVY, {**ORIGINAL, **HEAVY})
    key_error = "KeyError: 'api_key'\nmodel_config = {...}\n>   key = model_config['api_key']"
    baseline = run(
        (T_ASK, "passed"),
        (T_STRIP, "passed"),
        (T_LIVE, "failed", key_error),
        ("tests.test_heavy", "error", IMPORT_ERROR, "nlpaug"),
    )
    fixed_run = run(
        (T_ASK, "passed"),
        (T_STRIP, "passed"),
        (T_LIVE, "failed", "KeyError: 'api_key'\n(after the fix)"),
        ("tests.test_heavy", "error", IMPORT_ERROR, "nlpaug"),
        coverage=FULL_COVERAGE,
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [fixed_run]))
    assert v.delta.no_signal_preexisting == [T_LIVE, "tests.test_heavy"]
    assert v.delta.no_signal_messages == {
        T_LIVE: {"baseline": key_error, "fixed": "KeyError: 'api_key'\n(after the fix)"},
        "tests.test_heavy": {"baseline": IMPORT_ERROR, "fixed": IMPORT_ERROR},
    }
    data = json.loads(json.dumps(v.to_dict()))
    assert data["tests"]["no_signal_messages"][T_LIVE]["fixed"].endswith("(after the fix)")
    md = v.report_markdown()
    assert f"- `{T_LIVE}`: already failing before the fix: `KeyError: 'api_key'`\n" in md
    # pytest's "collection failure" line says nothing: the line after it does
    assert (
        "- `tests.test_heavy`: already failing before the fix: `ImportError while importing "
        "test module '/work/tests/test_heavy.py'.`\n"
    ) in md


@pytest.mark.parametrize(
    ("message", "shown"),
    [
        ("", ""),
        ("\n  \n", ""),
        ("collection failure", ": `collection failure`"),
        ("  AssertionError: `x` != 1\nmore", ": `` AssertionError: `x` != 1 ``"),
        ("E" * 200, ": `" + "E" * 157 + "...`"),
    ],
)
def test_report_first_line_of_a_no_signal_failure(message, shown):
    delta = TestDelta(no_signal_preexisting=["m::a"])
    delta.no_signal_messages["m::a"] = {"baseline": message, "fixed": "other"}
    assert _why_failing(delta, "m::a") == shown
    assert _why_failing(TestDelta(), "m::a") == ""


def test_module_collecting_only_after_the_fix_is_fixed_by_change(trees):
    original, fixed = trees(HEAVY, {**ORIGINAL, **HEAVY})
    baseline = run((T_ASK, "passed"), (T_STRIP, "passed"), ("tests.test_heavy", "error", "boom"))
    fixed_run = run(
        (T_ASK, "passed"),
        (T_STRIP, "passed"),
        ("tests.test_heavy::test_augment", "passed"),
        coverage=FULL_COVERAGE,
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [fixed_run]))
    assert v.delta.fixed_by_change == ["tests.test_heavy"]
    assert v.delta.new_tests == ["tests.test_heavy::test_augment"]


def test_module_that_failed_to_collect_and_is_gone_is_not_fixed_by_change(trees):
    original, fixed = trees({}, {**ORIGINAL, **HEAVY})  # the fix has no tests/test_heavy.py
    baseline = run((T_ASK, "passed"), (T_STRIP, "passed"), ("tests.test_heavy", "error", "boom"))
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [fixed_run]))
    assert v.delta.fixed_by_change == [] and v.delta.no_signal_preexisting == []


@pytest.mark.parametrize(
    "others",
    [
        [],
        [(T_ASK, "skipped")],
        [(T_ASK, "error", "openai.error.APIConnectionError: Connection error.")],
        [(T_ASK, "error", "collection failure", "app")],  # a repo module: still an import error
    ],
)
def test_environment_incomplete_when_nothing_else_usable_ran(trees, others):
    original, fixed = trees({})
    baseline = run(("tests.test_heavy", "error", NO_NLPAUG, "nlpaug"), *others)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [PASSING]))
    assert "environment_incomplete" in codes(v) and "missing_modules" not in codes(v)


def test_missing_modules_are_info_when_another_test_fails_for_a_reason_a_fix_can_change(trees):
    # nothing passed, but test_ask fails on the original code itself: the fix can be judged
    original, fixed = trees({})
    baseline = run(
        ("tests.test_heavy", "error", NO_NLPAUG, "nlpaug"),
        (T_ASK, "failed", "AssertionError: assert None == 'hi'"),
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [PASSING]))
    assert "environment_incomplete" not in codes(v)
    r = reason(v, "missing_modules")
    assert "nlpaug blocks tests.test_heavy; the other tests ran" in r.message  # no such file


def test_a_baseline_without_import_errors_is_not_environment_incomplete(trees):
    original, fixed = trees({})
    baseline = run((T_ASK, "failed", "assert 1 == 2"), (T_STRIP, "failed", "boom"))
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [PASSING]))
    assert not codes(v) & {"environment_incomplete", "missing_modules"}


def missing_library_run(**declared) -> SandboxResult:
    fixed_run = run(("tests.test_app", "error", "collection failure", "openai"))
    fixed_run.smoke = SmokeResult(
        "failed",
        'import openai; openai.OpenAI(api_key="<dummy key>")',
        "ModuleNotFoundError: No module named 'openai'",
        "openai",
        None,
    )
    fixed_run.dependency_sources = ["pyproject.toml"]
    fixed_run.declared = {k: list(v) for k, v in declared.items()}
    return fixed_run


def test_smoke_check_says_when_nothing_declares_the_library(trees):
    original, fixed = trees({})
    fixed_run = missing_library_run()
    fixed_run.not_installed = [
        NotInstalled("openai", "environment.yml (pip)", "a pixi manifest takes precedence")
    ]
    baseline = run(("tests.test_app", "error", "collection failure", "openai"))
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [fixed_run]))
    assert "smoke_check_failed" not in codes(v)
    r = reason(v, "library_not_declared")
    assert r.severity == "unverified" and v.verdict == "unverified"
    assert r.message.startswith(
        "openai is not installed: no dependency file apifix reads declares it (read: "
        "pyproject.toml); declared but not installed: environment.yml (pip) (a pixi manifest "
        "takes precedence). If the project declares it somewhere else, pass "
        "--sandbox-requirements FILE"
    )
    assert "raise or drop" not in r.message
    assert "library_not_declared" not in "\n".join(feedback_lines(v))


def test_smoke_check_says_when_the_fix_removed_the_library(trees):
    original, fixed = trees({})
    baseline = run((T_ASK, "passed"), (T_STRIP, "passed"))
    baseline.declared = {"openai": ["requirements.txt"]}
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [missing_library_run()]))
    r = reason(v, "library_removed")
    assert r.severity == "reject" and v.verdict == "rejected"
    assert "the fix's dependency files no longer declare it (the original declared it in " in (
        r.message
    )
    assert "library_removed" in "\n".join(feedback_lines(v))


def test_declared_but_uninstalled_library_keeps_the_dependency_message(trees):
    original, fixed = trees({})
    fixed_run = missing_library_run(openai=["requirements.txt"])
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [fixed_run]))
    r = reason(v, "smoke_check_failed")
    assert "Installed: openai not installed" in r.message


def test_report_lists_dependencies_left_out_and_the_override(trees):
    original, fixed = trees({})
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    conda = ("pyproject.toml [tool.pixi.dependencies]", "conda-only package; not supported yet")
    fixed_run.not_installed = [NotInstalled(name, *conda) for name in ("typer", "spotipy")]
    fixed_run.requirements_override = "--sandbox-requirements reqs.txt"
    fixed_run.override_replaced = ["openai: openai==0.28 -> openai>=1.0"]
    sandbox = FakeSandbox(PASSING, [fixed_run])
    v = verify(original, fixed, "openai-v1", sandbox=sandbox)
    md = v.report_markdown()
    assert "### Declared dependencies not installed" in md
    assert (
        "- `pyproject.toml [tool.pixi.dependencies]`: typer, spotipy: not installed "
        "(conda-only package; not supported yet)" in md
    )
    assert (
        "**Dependencies:** from `--sandbox-requirements reqs.txt`, replacing apifix's "
        "detection for the original code and the fix. For the fix, its own dependency edits "
        "replaced: `openai: openai==0.28 -> openai>=1.0`" in md
    )
    summary = v.to_dict()["runs"]["fixed"]
    assert summary["not_installed"][0] == {"name": "typer", "source": conda[0], "why": conda[1]}
    assert summary["override_replaced"] == ["openai: openai==0.28 -> openai>=1.0"]
    assert sandbox.originals == [original.resolve()]


# --- review by hand ---------------------------------------------------------------------------

SETTINGS_OLD = '''\
import openai

TIMEOUT = 5
PROMPT = """Say hello
politely."""


def setup(key):
    openai.api_key = key
    return key
'''
SETTINGS_NEW = '''\
import openai

PROMPT = """Say hello
kindly."""


def setup(key):
    # OpenAI() reads the key from the environment
    return key
'''


def _review_trees(trees):
    return trees({"settings.py": SETTINGS_NEW}, {**ORIGINAL, "settings.py": SETTINGS_OLD})


def test_review_by_hand_lists_hunks_no_test_executed(trees):
    original, fixed = _review_trees(trees)
    coverage = {
        "app.py": FileCoverage(frozenset({1, 3, 6}), frozenset({7, 8})),
        "settings.py": FileCoverage(frozenset({1, 3, 7, 9}), frozenset()),  # all of it ran
    }
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=coverage)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert v.verdict == "verified_weak"
    # app.py's client line ran (not listed); an edit inside a string, a removal and code turned
    # into a comment have no executable line, so coverage cannot vouch for them
    assert [(e["file"], e["line"], e["end_line"]) for e in v.review_by_hand] == [
        ("app.py", 7, 8),
        ("settings.py", 3, 3),
        ("settings.py", 4, 4),
        ("settings.py", 8, 8),
    ]
    report = v.report_markdown()
    section = report.split("### Review by hand\n\nNo test executed these changes:\n\n")[1]
    assert section.startswith(
        '- `app.py:7-8`: `r = openai.ChatCompletion.create(model="gpt-4", messages=[{"role": '
        '"user", "content": q}])` -> `r = client.chat.completions.create(model="gpt-4", '
        'messages=[{"role": "user", "content": q}])` (+1 line)\n'
        "- `settings.py:3`: removed `TIMEOUT = 5`\n"
        '- `settings.py:4`: `politely."""` -> `kindly."""`\n'
        "- `settings.py:8`: removed `openai.api_key = key`; added "
        "`# OpenAI() reads the key from the environment`\n"
    )
    assert v.to_dict()["review_by_hand"] == v.review_by_hand


def test_review_by_hand_is_capped(trees):
    import dataclasses

    original, fixed = _review_trees(trees)
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=None)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert v.verdict == "verified_weak"  # coverage unavailable: every hunk is listed
    assert len(v.review_by_hand) == 5
    many = dataclasses.replace(v, review_by_hand=v.review_by_hand * 4)
    report = many.report_markdown()
    assert report.count("\n- `settings.py:") + report.count("\n- `app.py:") == 15
    assert "- ... and 5 more (see fix.diff)" in report


def test_review_by_hand_only_for_weak_and_unverified_results(trees):
    original, fixed = _review_trees(trees)
    full = {
        "app.py": FileCoverage(frozenset({1, 3, 6, 7, 8}), frozenset()),
        "settings.py": FileCoverage(frozenset({1, 3, 7, 9}), frozenset()),
    }
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=full)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert v.verdict == "verified" and v.review_by_hand == []
    assert "Review by hand" not in v.report_markdown()
    nothing = run(status="unverified", reason="no test files found")
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(nothing, [nothing]))
    assert v.verdict == "unverified" and len(v.review_by_hand) == 5


RETRY = """\
def call_with_retry(func, retries, **kwargs):
    return func(**kwargs)
"""
WRAPPED_OLD = """\
import openai

from retry import call_with_retry


def ask(q):
    r = call_with_retry(
        func=openai.Completion.create,
        retries=3,
        engine="davinci",
        prompt=q,
    )
    return r["choices"][0]["text"]
"""
# the call is migrated, the engine= line (10) is left as it was
WRAPPED_NEW = """\
import openai

from retry import call_with_retry


def ask(q):
    r = call_with_retry(
        func=openai.OpenAI(api_key="test").completions.create,
        retries=3,
        engine="davinci",
        prompt=q,
    )
    return r.choices[0].text
"""


def test_kwarg_left_on_an_unchanged_line_is_incomplete(trees):
    original_files = {**ORIGINAL, "retry.py": RETRY, "app.py": WRAPPED_OLD}
    original, fixed = trees({"retry.py": RETRY, "app.py": WRAPPED_NEW}, original_files)
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=None)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    r = reason(v, "incomplete_migration")
    assert (r.file, r.line) == ("app.py", 10)
    assert 'engine="davinci"' in r.message and "pass model=" in r.message
    (change,) = [ch for ch in v.changes if ch.path == "app.py"]
    assert 10 not in change.changed_lines
    assert v.verdict == "rejected"


def test_review_by_hand_lists_remaining_findings_on_unchanged_lines(trees):
    settings = 'import openai\n\nopenai.api_type = "open_ai"\n'
    original_files = {**ORIGINAL, "settings.py": settings}
    original, fixed = trees({"settings.py": settings}, original_files)
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=None)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert v.verdict == "verified_weak"
    assert [(e["file"], e["line"], e["changed"]) for e in v.review_findings] == [
        ("settings.py", 3, False)
    ]
    assert v.to_dict()["review_findings"] == v.review_findings
    section = v.report_markdown().split("### Review by hand\n\n")[1]
    unchanged = "Migration findings still in the code:\n\n- `settings.py:3` (line not changed by"
    assert unchanged in section
    assert "client_config [likely] `openai.api_type` -> " in section


def test_review_findings_mark_changed_lines_and_only_weak_verdicts(trees):
    settings_old = 'import openai\n\nopenai.api_type = "open_ai"\n'
    settings_new = 'import openai\n\nopenai.api_type = "azure"\n'
    original_files = {**ORIGINAL, "settings.py": settings_old}
    original, fixed = trees({"settings.py": settings_new}, original_files)
    after = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=None)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [after]))
    assert [(e["line"], e["changed"]) for e in v.review_findings] == [(3, True)]
    assert "`settings.py:3`: client_config" in v.report_markdown()
    full = {
        "app.py": FileCoverage(frozenset({1, 3, 6, 7, 8}), frozenset()),
        "settings.py": FileCoverage(frozenset({1, 3}), frozenset()),
    }
    covered = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=full)
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [covered]))
    assert v.verdict == "verified" and v.review_findings == []


def test_review_line_forms():
    from apifix.engine.verifier import review_line

    base = {"file": "a.py", "line": 3, "end_line": 3, "old": "x = 1", "new": "x = 2"}
    one = {**base, "old_lines": 1, "new_lines": 1, "new_is_code": True}
    assert review_line(one) == "`a.py:3`: `x = 1` -> `x = 2`"
    added = {**one, "old_lines": 0, "new_lines": 3, "end_line": 5, "old": ""}
    assert review_line(added) == "`a.py:3-5`: added `x = 2` (+2 lines)"
    removed = {**one, "old_lines": 2, "new_lines": 0, "new": ""}
    assert review_line(removed) == "`a.py:3`: removed `x = 1` (+1 line)"
    ticks = {**one, "new": "print(`x`)"}
    assert review_line(ticks) == "`a.py:3`: `x = 1` -> `` print(`x`) ``"


# --- conflicting dependency sources and pip conflicts (docs/plans/llmflows-run-1.md) -----------


def test_report_and_summary_show_dropped_sources_and_pip_conflicts(trees):
    from apifix.engine.project import DroppedSource

    original, fixed = trees({})
    dropped = DroppedSource(
        "pyproject.toml",
        "requirements.txt",
        "the repo's CI installs it: .github/workflows/test.yml",
        (("tiktoken==0.5.1", "tiktoken==0.3.3"),),
    )
    baseline = run(status="error", reason="dependency install failed")
    baseline.dropped_sources = [dropped]
    baseline.install_conflict = [
        "The conflict is caused by:",
        "    The user requested anthropic==0.3.11",
        "    openai 1.55.3 depends on httpx<1,>=0.23.0",
    ]
    baseline.declared = {"anthropic": ["requirements.txt"], "openai": ["requirements.txt"]}
    fixed_run = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=FULL_COVERAGE)
    fixed_run.dropped_sources = [dropped]
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(baseline, [fixed_run]))
    md = v.report_markdown()
    assert md.count("**Dependency sources:**") == 1
    assert (
        "**Dependency sources:** `pyproject.toml` pins tiktoken==0.5.1 where `requirements.txt` "
        "pins tiktoken==0.3.3: installed from `requirements.txt` (the repo's CI installs it: "
        ".github/workflows/test.yml), not `pyproject.toml`." in md
    )
    assert (
        "### Dependency install conflict\n\n"
        "pip could not install the original code's dependencies (pip's output, untrusted):\n\n"
        "```\nThe conflict is caused by:\n    The user requested anthropic==0.3.11\n"
        "    openai 1.55.3 depends on httpx<1,>=0.23.0\n```\n\n"
        "- `anthropic` is declared in `requirements.txt`\n" in md
    )
    runs = v.to_dict()["runs"]
    assert runs["baseline"]["install_conflict"] == baseline.install_conflict
    assert runs["fixed"]["dropped_sources"] == [
        {
            "file": "pyproject.toml",
            "kept": "requirements.txt",
            "why": "the repo's CI installs it: .github/workflows/test.yml",
            "pins": [["tiktoken==0.5.1", "tiktoken==0.3.3"]],
        }
    ]
