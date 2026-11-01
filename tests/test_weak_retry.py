"""An extra attempt before settling for verified_weak, for migrated tests that still fail."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from apifix.engine.diffing import FileChange
from apifix.engine.test_results import FileCoverage
from apifix.engine.verifier import TestDelta
from apifix.engine.weak_retry import (
    patches,
    retry_prompt,
    skip_reason,
    split_environmental,
    still_failing_migrated,
)
from tests.test_agent import (
    FIX_APP,
    FIX_TESTS,
    FakeLLM,
    JudgingSandbox,
    block,
    done,
    edit,
    go,
    judge,
    tools,
    wf,
)
from tests.test_verifier import APP_NEW, ORIGINAL, TESTS_NEW, run, write_tree

STILL = "tests.test_app::test_extra"
MARK = "# EXTRA-FIXED"
MESSAGE = "AssertionError: assert fake_answer == 'extra'"


def judging(extra: str = STILL, *, fixed_by: str = MARK):
    """The fake test run plus one test that fails until `fixed_by` is in app.py or the tests,
    and a fix whose changed lines no test executes (verified_weak)."""

    def judge_with_extra(tree: Path):
        r = judge(tree)
        text = (tree / "app.py").read_text() + (tree / "tests" / "test_app.py").read_text()
        outcome = ("passed",) if fixed_by in text else ("failed", MESSAGE)
        weak = run(*[(t.id, t.outcome, t.message) for t in r.tests], (extra, *outcome))
        weak.coverage = {"app.py": FileCoverage(frozenset({1}), frozenset(range(2, 30)))}
        return weak

    return JudgingSandbox(judge=judge_with_extra)


@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path / "repo", ORIGINAL)


def attempts(result):
    return [a["result"] for a in result.trajectory["attempts"]]


def test_an_extra_attempt_fixes_the_migrated_tests_that_still_fail(repo):
    def second(messages):
        text = messages[-1].text
        assert "accepted attempt 1 only with weak evidence" in text
        assert f"- {STILL} (in tests/test_app.py, which the fix edited)" in text
        assert MESSAGE in text and "do not weaken, skip or delete tests" in text
        return tools(edit("app.py", "import openai\n", f"import openai  {MARK}\n"))

    llm = FakeLLM([tools(FIX_APP, FIX_TESTS), done(), second, done()])
    result = go(repo, llm, judging())
    assert result.outcome == "verified_weak" and result.attempts == 2
    assert result.reason == "verified_weak on attempt 2"
    assert MARK in result.diff
    first, extra = result.trajectory["attempts"]
    assert first["weak_tests"] == {STILL: "in tests/test_app.py, which the fix edited"}
    assert "attempt 1 was verified_weak" in first["weak_retry"] and STILL in first["weak_retry"]
    assert extra["weak_tests"] == {} and extra["weak_retry"] == ""
    line = next(ln for ln in result.report_markdown.splitlines() if "Extra attempt 2" in ln)
    assert "before settling for weak evidence" in line and "it ended verified_weak" in line


def test_a_worse_extra_attempt_keeps_the_first_weak_result(repo):
    broken = edit("app.py", "client = ", "# BREAK\nclient = ")
    llm = FakeLLM([tools(FIX_APP, FIX_TESTS), done(), tools(broken), done()])
    result = go(repo, llm, judging(), max_retries=1)
    assert attempts(result) == ["verified_weak", "rejected"]
    assert result.outcome == "verified_weak"
    assert result.reason == "verified_weak on attempt 1; attempt(s) 2 did not improve on it"
    assert "BREAK" not in result.diff and result.trajectory["chosen_attempt"] == 1
    assert "Extra attempt 2" in result.report_markdown


def test_an_extra_attempt_that_leaves_the_same_tests_failing_is_not_better(repo):
    again = edit("app.py", "import openai\n", "import openai  # tried\n")
    llm = FakeLLM([tools(FIX_APP, FIX_TESTS), done(), tools(again), done()])
    result = go(repo, llm, judging())
    assert attempts(result) == ["verified_weak", "verified_weak"]  # no third: no progress
    assert result.reason == "verified_weak on attempt 1; attempt(s) 2 did not improve on it"
    assert "# tried" not in result.diff and result.trajectory["chosen_attempt"] == 1


def test_no_extra_attempt_when_none_is_left(repo):
    result = go(repo, FakeLLM([tools(FIX_APP, FIX_TESTS), done()]), judging(), max_retries=0)
    assert (result.outcome, result.attempts) == ("verified_weak", 1)
    first = result.trajectory["attempts"][0]
    assert first["weak_tests"] and first["weak_retried"] is False  # still recorded
    assert first["weak_retry"].startswith("not retried: no attempt is left")
    assert "No extra attempt before settling for weak evidence: not retried" in (
        result.report_markdown
    )


def test_no_extra_attempt_for_a_failing_test_the_fix_did_not_touch(repo):
    llm = FakeLLM([tools(FIX_APP, FIX_TESTS), done()])
    result = go(repo, llm, judging("tests.test_other::test_y"))
    assert (result.outcome, result.attempts) == ("verified_weak", 1)
    assert result.trajectory["attempts"][0]["weak_tests"] == {}


def test_whole_file_mode_sends_the_failing_tests_file_again(repo):
    fixed_by = "def make_completion("  # a code change, not a comment: those are reverted
    tests_again = TESTS_NEW.replace("completion(", "make_completion(")
    llm = FakeLLM([block(APP_NEW), block(TESTS_NEW), block(tests_again)])
    result = wf(repo, llm, judging(fixed_by=fixed_by))
    assert result.outcome == "verified_weak" and result.attempts == 2
    assert result.reason == "verified_weak on attempt 2"
    assert result.trajectory["attempts"][0]["resend"] == {
        "tests/test_app.py": f"{STILL} still fails (in tests/test_app.py, which the fix edited)"
    }
    assert "Migrate this file: tests/test_app.py" in llm.requests[2][0].text
    assert fixed_by in result.diff


def test_still_failing_migrated_finds_edited_and_patching_test_files(tmp_path):
    root = write_tree(
        tmp_path,
        {
            "app.py": "x = 1\n",
            "tests/test_a.py": "def test_a(): pass\n",
            "tests/test_b.py": 'from unittest import mock\n@mock.patch("app.client.chat")\n'
            "def test_b(chat): pass\n",
            "tests/test_c.py": "def test_c(monkeypatch):\n"
            '    monkeypatch.setattr("os.getcwd", 1)\n',
        },
    )
    delta = TestDelta(
        no_signal_preexisting=[
            "tests.test_a::test_a",
            "tests.test_b::test_b",
            "tests.test_c::test_c",
        ]
    )
    changes = [FileChange("app.py", "modified"), FileChange("tests/test_a.py", "modified")]
    v = SimpleNamespace(changes=changes, delta=delta)
    found = still_failing_migrated(v, root, {"openai"})
    assert list(found) == ["tests.test_a::test_a", "tests.test_b::test_b"]
    assert found["tests.test_b::test_b"] == "in tests/test_b.py, which patches code the fix rewrote"
    # an edited file is not needed when only the migrated library is patched
    assert patches(root / "tests/test_c.py", ["os"]) and not patches(
        root / "tests/test_c.py", ["a"]
    )
    assert not patches(root / "missing.py", ["app"])


def test_retry_prompt_cuts_long_output(repo):
    long = SimpleNamespace(id=STILL, message="x" * 5000)
    v = SimpleNamespace(fixed=SimpleNamespace(tests=[long]))
    text = retry_prompt(2, v, {STILL: "why"})
    assert "attempt 2 only with weak evidence" in text and "(output truncated)" in text
    assert len(text) < 2500


def import_error(module: str, through: str = "") -> str:
    """The shape of the output in ailabel-genai-4: a pytest collection failure."""
    return (
        "collection failure\nImportError while importing test module '/work/tests/test_cli.py'.\n"
        "Traceback:\ntests/test_cli.py:6: in <module>\n    from pkg.cli import app\n"
        + (f"{through}:16: in <module>\n    from x import y\n" if through else "")
        + f"E   ModuleNotFoundError: No module named '{module}'"
    )


def collection_v(message, edited=("pkg/llms.py",)):
    return SimpleNamespace(
        changes=[FileChange(p, "modified") for p in edited],
        delta=TestDelta(
            no_signal_preexisting=["tests.test_cli"],
            no_signal_messages={"tests.test_cli": {"baseline": message, "fixed": message}},
        ),
        fixed=SimpleNamespace(tests=[]),
    )


def test_collection_import_errors_are_split_by_whether_the_migration_is_involved(tmp_path):
    root = write_tree(tmp_path, {"pkg/__init__.py": "", "pkg/cli.py": "", "pkg/llms.py": ""})
    tests = {"tests.test_cli": "why"}

    def split(message, edited=("pkg/llms.py",)):
        return split_environmental(
            collection_v(message, edited), root, {"google.generativeai"}, tests
        )

    assert split(import_error("pkg.label_prediction")) == (tests, {})  # the repo's own module
    assert split(import_error("google.generativeai"))[1] == {}  # the migrated library
    assert split(import_error("requests", through="pkg/llms.py"))[1] == {}  # a changed file
    assert split("collection failure\nImportError while importing test module 'x'.\nTrace")[1] == {}
    assert split(import_error("requests")) == ({}, {"tests.test_cli": "requests"})  # unrelated


def test_retry_prompt_shows_a_collection_failure(tmp_path):
    v = collection_v(import_error("pkg.label_prediction"))
    text = retry_prompt(2, v, {"tests.test_cli": "why"})
    assert "No module named 'pkg.label_prediction'" in text


def test_the_skip_is_recorded_when_no_extra_attempt_is_made():
    text = skip_reason("no attempt is left", {"a": "x", "b": "y"})
    assert text == "not retried: no attempt is left (2 test(s) still failing: `a`, `b`)"


def test_whole_file_resends_the_edited_files_when_the_failing_test_file_is_not_sendable(tmp_path):
    """A test file that patches rewritten code but is not itself sent (and imports nothing
    sendable) still gets a retry: the files the fix edited are sent again."""
    other = 'from unittest import mock\n\n@mock.patch("app.client.chat")\ndef test_y(chat): pass\n'
    repo = write_tree(tmp_path / "repo", {**ORIGINAL, "tests/test_other.py": other})
    fixed_by = "def make_ask("
    app_again = APP_NEW.replace("def ask(", "def make_ask(")
    llm = FakeLLM([block(APP_NEW), block(TESTS_NEW), block(app_again), block(TESTS_NEW)] * 3)
    result = wf(repo, llm, judging("tests.test_other::test_y", fixed_by=fixed_by))
    first = result.trajectory["attempts"][0]
    assert first["weak_tests"] and first["weak_retried"] is True
    assert result.attempts >= 2
    assert first["resend"]["app.py"].startswith("edited by the fix")
