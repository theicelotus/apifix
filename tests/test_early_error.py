"""genai-gptoss20b-3 (docs/plans/genai-run-9.md): a mock target on the guide's sample module
(`app.OpenAI`), a call assertion added to a test that never reaches the call, the hint for an
uncalled mock in a test that patches the client class, and resending only the files a
rejection implicates (agent.implicated_files)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from apifix.engine.agent import implicated_files
from apifix.engine.code_checks import (
    added_call_assertions,
    mock_target_problems,
    module_roots,
    patches_client_class,
)
from apifix.engine.imports import importable_modules
from apifix.engine.migrations import get_migration
from apifix.engine.models import Finding
from apifix.engine.scanner import scan
from apifix.engine.verifier import Reason, TestDelta, Verification
from apifix.engine.whole_file import check_candidate
from tests.test_code_checks import reader
from tests.test_unkeyed_patches import STUB
from tests.test_verifier import run

MIGRATION = get_migration("openai-v1")
OPENAI = frozenset({"openai"})
FIXTURE = Path(__file__).parent / "fixtures" / "early_error"
HANDLER_TEST = "tests/test_handler.py"
MAGICS_TEST = "tests/test_magics.py"
MODULES = (
    "cellhelp/__init__.py",
    "cellhelp/display.py",
    "cellhelp/generate.py",
    "cellhelp/handler.py",
    "cellhelp/magics.py",
)
ORIGINAL = {
    rel: (FIXTURE / rel).read_text()
    for rel in (*MODULES, HANDLER_TEST, MAGICS_TEST, "requirements.txt")
}
IMPORTABLE = importable_modules(FIXTURE, None, MIGRATION.api_modules | MIGRATION.companion_modules)

# cellhelp/generate.py migrated as in the run: `from openai import OpenAI`, a client per call
GENERATE = '''\
"""Suggestions from the chat API."""

from openai import OpenAI


def suggest(error: str) -> str:
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": f"Explain this error: {error}"}],
    )
    return response.choices[0].message.content
'''
REPLY = (
    "SimpleNamespace(\n        choices=[SimpleNamespace(message=SimpleNamespace("
    'role="assistant", content="Define b"))]\n    )'
)
# the run's test_suggestions.py shape: the client class patched where generate.py looks it up
# (right), and a call assertion added to the test whose display raises first (wrong)
HANDLER_RUN = f"""\
from types import SimpleNamespace
from unittest import mock

from cellhelp.handler import on_error


@mock.patch("cellhelp.display.show")
@mock.patch("cellhelp.generate.OpenAI")
def test_on_error(OpenAI, show):
    OpenAI.return_value.chat.completions.create.return_value = {REPLY}
    on_error("NameError: b")
    OpenAI.return_value.chat.completions.create.assert_called_once()
    show.assert_called_with("Define b")


@mock.patch("cellhelp.display.show", side_effect=TypeError("'Fun' is not allowed"))
@mock.patch("builtins.print")
@mock.patch("cellhelp.generate.OpenAI")
def test_on_error_inside(OpenAI, print, show):
    OpenAI.return_value.chat.completions.create.return_value = {REPLY}
    on_error("NameError: b")
    assert print.call_args.args[0] == "Error while trying to provide a suggestion: "
    assert print.call_args.args[1].args[0] == "'Fun' is not allowed"

    OpenAI.return_value.chat.completions.create.assert_called_once()
"""
HANDLER_FIX = HANDLER_RUN.replace(
    "\n\n    OpenAI.return_value.chat.completions.create.assert_called_once()\n", "\n"
)
# the run's test_magics.py shape: the guide's sample module as the target
MAGICS_RUN = """\
from types import SimpleNamespace
from unittest import mock

from cellhelp.magics import assist


@mock.patch("app.OpenAI")
def test_assist(OpenAI):
    message = SimpleNamespace(role="assistant", content="superplot(df)")
    OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=message)]
    )
    assert assist("plot df") == "superplot(df)"
    OpenAI.return_value.chat.completions.create.assert_called_once()
"""
MAGICS_FIX = MAGICS_RUN.replace('"app.OpenAI"', '"cellhelp.generate.OpenAI"')

APP_PROBLEM_HEAD = (
    "mock target 'app.OpenAI': there is no module 'app' (not a repo module, the standard "
    "library, a declared dependency or a module the repo imports), so patch() fails with "
    "ModuleNotFoundError: No module named 'app'. A module name in an example is a placeholder: "
    "use the dotted path the tests import the code under test by. Patch openai where a module "
    "the test reaches uses it:\n"
    '    cellhelp/generate.py: suggest() creates client = OpenAI(): patch("cellhelp.generate.'
    'OpenAI") and set OpenAI.return_value.chat.completions.create.return_value.\n'
)
ADDED_PROBLEM = (
    "line 29: test_on_error_inside adds a call assertion the original test does not make: "
    "`OpenAI.return_value.chat.completions.create.assert_called_once()` (the original makes "
    "none). Migrate the test's existing assertions and add no new ones: the original test may "
    "not reach that call (an error raised earlier, a branch not taken), so the new assertion "
    "fails"
)


def tree(changed: dict[str, str] | None = None) -> dict[str, str]:
    """The fixture with generate.py migrated, plus `changed` files."""
    return {**ORIGINAL, "cellhelp/generate.py": GENERATE, **(changed or {})}


def targets(path: str, candidate: str, importable=IMPORTABLE) -> list[str]:
    return mock_target_problems(
        ORIGINAL[path],
        candidate,
        reader(tree({path: candidate})),
        library_modules=OPENAI,
        mock_responses=MIGRATION.mock_responses,
        roots=module_roots(path),
        modules=MODULES,
        test_files=(HANDLER_TEST, MAGICS_TEST),
        path=path,
        importable=importable,
    )


# --- in a sandbox-like run: both failures as the run had them, and the fixes -----------------


def _pytest(root: Path, changed: dict[str, str]) -> subprocess.CompletedProcess:
    for rel, text in {**STUB, **tree(changed)}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        env={**env, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_runs_shapes_fail_as_in_the_sandbox_and_the_fixes_pass(tmp_path):
    result = _pytest(tmp_path / "run", {HANDLER_TEST: HANDLER_RUN, MAGICS_TEST: MAGICS_RUN})
    assert result.returncode == 1
    assert "2 failed, 1 passed" in result.stdout
    assert "ModuleNotFoundError: No module named 'app'" in result.stdout
    assert "Expected 'create' to have been called once. Called 0 times." in result.stdout
    fixed = _pytest(tmp_path / "fix", {HANDLER_TEST: HANDLER_FIX, MAGICS_TEST: MAGICS_FIX})
    assert fixed.returncode == 0, fixed.stdout


# --- (a) a target on a module that does not exist ---------------------------------------------


def test_a_target_on_the_sample_module_is_rejected_with_the_real_module():
    (problem,) = targets(MAGICS_TEST, MAGICS_RUN)
    assert problem.startswith(APP_PROBLEM_HEAD)
    assert '    @mock.patch("cellhelp.generate.OpenAI")\n    def test_assist(OpenAI):' in problem
    assert targets(MAGICS_TEST, MAGICS_FIX) == []
    # the guide's placeholder is no module either
    (problem,) = targets(MAGICS_TEST, MAGICS_RUN.replace('"app.', '"yourpkg.module.'))
    assert problem.startswith("mock target 'yourpkg.module.OpenAI': there is no module 'yourpkg'")
    # not checked without the importable names (the check stays opt-in)
    assert targets(MAGICS_TEST, MAGICS_RUN, importable=None) == []


def test_targets_on_modules_the_test_run_can_import_pass():
    for target in (
        "builtins.print",  # stdlib
        "cellhelp.display.show",  # a repo module
        "openai.resources.chat.completions.Completions.create",  # the library
        "httpx.Client.send",  # its companion
        "pytest.fail",  # declared in requirements.txt
    ):
        candidate = ORIGINAL[MAGICS_TEST].replace('"openai.ChatCompletion.create"', f'"{target}"')
        assert targets(MAGICS_TEST, candidate) == [], target


def test_importable_modules(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "util.py").write_text("import requests\nfrom yaml import safe_load\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run.py").write_text("")  # a dir without __init__.py
    (repo / "requirements.txt").write_text("python-dotenv\nPillow\n")
    names = importable_modules(repo, None, frozenset({"openai"}))
    for name in ("os", "sys", "builtins", "pkg", "util", "scripts", "run", "requests", "yaml"):
        assert names(name), name
    for name in ("dotenv", "PIL", "pytest", "openai"):  # declared, always installed, extra
        assert names(name), name
    for name in ("app", "yourpkg", "numpy"):
        assert not names(name), name
    dotted = importable_modules(repo, None, frozenset({"google.genai", "google.generativeai"}))
    assert dotted("google")  # patch("google.genai.Client") starts with an importable name


# --- the call assertion the run added ---------------------------------------------------------


def test_a_call_assertion_added_to_a_test_is_rejected():
    assert added_call_assertions(ORIGINAL[HANDLER_TEST], HANDLER_RUN) == [ADDED_PROBLEM]
    # test_on_error's create.assert_called_once() is migrated, not added
    assert added_call_assertions(ORIGINAL[HANDLER_TEST], HANDLER_FIX) == []


def test_call_assertions_are_aligned_by_kind():
    original = (
        "def test_a(create):\n    run()\n    assert create.called\n\n\n"
        "class TestB:\n    def test_b(self, create):\n        run()\n"
    )
    # `assert create.called` -> `create.assert_called()`: changed, not added
    changed = original.replace("assert create.called", "create.assert_called()")
    assert added_call_assertions(original, changed) == []
    # two new ones in a method; a new test may assert what it likes
    more = original.replace(
        "        run()\n",
        "        run()\n        create.assert_called_once()\n        create.assert_any_call(1)\n",
    )
    more += "\n\ndef test_new(create):\n    create.assert_called_once()\n"
    (problem,) = added_call_assertions(original, more)
    assert problem.startswith(
        "line 9: TestB.test_b adds a call assertion the original test does not make: "
        "`create.assert_called_once()` (and 1 more) (the original makes none)."
    )
    # one more than the original's one
    extra = original.replace(
        "    assert create.called\n", "    assert create.called\n    create.assert_awaited()\n"
    )
    (problem,) = added_call_assertions(original, extra)
    assert "`create.assert_awaited()` (the original makes 1, on create)" in problem


def test_whole_file_checks_reject_the_runs_test_files():
    findings = scan(FIXTURE, "openai-v1").findings

    def checked(path: str, candidate: str):
        return check_candidate(
            path,
            ORIGINAL[path],
            candidate,
            required=False,
            read_module=reader(tree({path: candidate})),
            library_modules=OPENAI,
            module_roots=module_roots(path),
            python_modules=MODULES,
            test_files=(HANDLER_TEST, MAGICS_TEST),
            findings=[f for f in findings if f.file == path],
            client_classes=MIGRATION.client_classes,
            client_resources=MIGRATION.client_resources,
            client_key=MIGRATION.client_key,
            env_names=(MIGRATION.client_key.env,),
            importable=IMPORTABLE,
        )

    result = checked(HANDLER_TEST, HANDLER_RUN)
    assert (result.error_checks, result.errors) == (["call_assertions"], [ADDED_PROBLEM])
    result = checked(MAGICS_TEST, MAGICS_RUN)
    assert result.error_checks == ["mock_targets"]
    assert result.errors[0].startswith(APP_PROBLEM_HEAD)
    for path, fix in ((HANDLER_TEST, HANDLER_FIX), (MAGICS_TEST, MAGICS_FIX)):
        assert checked(path, fix).errors == [], path


# --- (b) the guide's sample module ------------------------------------------------------------


def test_the_guide_and_hints_use_a_placeholder_module():
    texts = [MIGRATION.guide, *(text for _, text in MIGRATION.failure_hints)]
    for text in texts:
        assert '"app.' not in text and "app.py" not in text and "from app " not in text
    tests_part = MIGRATION.guide_text(["tests"])
    assert '@mock.patch("yourpkg.module.openai.OpenAI")' in tests_part
    assert "`yourpkg.module` in these examples is a placeholder" in tests_part
    assert "migrate the ones it has and add none" in tests_part


# --- the uncalled-mock hint for a test that patches the client class ---------------------------


def test_patches_client_class():
    classes = MIGRATION.client_classes
    assert patches_client_class(HANDLER_RUN, "test_on_error_inside", classes)
    assert not patches_client_class(ORIGINAL[HANDLER_TEST], "test_on_error_inside", classes)
    assert not patches_client_class(HANDLER_RUN, "test_missing", classes)
    method = "class TestA:\n    @mock.patch('app.openai.AsyncOpenAI')\n    def test_x(self, c):\n"
    assert patches_client_class(method + "        pass\n", "TestA.test_x", classes)


def test_the_patched_client_hint_names_the_tests():
    text = MIGRATION.uncalled_patched_hint.format(tests="tests.test_handler::test_on_error_inside")
    assert text.startswith(
        "a mock was never called in tests.test_handler::test_on_error_inside, which patch the "
        "client class, so no real client is built and no API key is needed"
    )
    assert "a call assertion the original does not make" in text


# --- resending only what the rejection implicates ---------------------------------------------

T_INSIDE = "tests.test_handler::test_on_error_inside"
T_ASSIST = "tests.test_magics::test_assist"
CANDIDATES = ["requirements.txt", "cellhelp/generate.py", HANDLER_TEST, MAGICS_TEST]
UNCALLED = "AssertionError: Expected 'create' to have been called once. Called 0 times."
NO_APP = "ModuleNotFoundError: No module named 'app'\nargs = ()\n    @wraps(func)"


def rejection(*tests, reasons=(), remaining=(), fixed=None) -> Verification:
    """A rejection whose regressions are `tests` ((id, outcome, message))."""
    fixed = fixed or run(*tests)
    failing = [t[0] for t in tests if t[1] == "failed"]
    regression = Reason("regression", "reject", "tests that passed before now fail", tuple(failing))
    return Verification(
        verdict="rejected",
        reasons=[regression, *reasons] if failing else list(reasons),
        delta=TestDelta(newly_failing=failing),
        edits=[],
        coverage=None,  # type: ignore[arg-type]
        changes=[],
        findings_before=10,
        remaining_findings=list(remaining),
        baseline=run(),
        fixed=fixed,
    )


def implicated(v: Verification, reaches=None, still_failing=()) -> dict[str, str]:
    graph = {HANDLER_TEST: ["cellhelp/generate.py"], MAGICS_TEST: ["cellhelp/generate.py"]}
    return implicated_files(
        v,
        CANDIDATES,
        FIXTURE,
        reaches=reaches or (lambda test: graph.get(test, [])),
        still_failing=still_failing,
    )


def test_the_runs_rejection_resends_only_the_two_test_files():
    v = rejection((T_ASSIST, "failed", NO_APP), (T_INSIDE, "failed", UNCALLED))
    info = Reason("setup_changed", "info", "test body changed", ("test_on_error",), HANDLER_TEST)
    v.reasons.append(info)  # info reasons implicate nothing
    assert implicated(v) == {
        HANDLER_TEST: f"failing test {T_INSIDE}",
        MAGICS_TEST: f"failing test {T_ASSIST}",
    }


def test_a_traceback_into_the_code_under_test_resends_it():
    message = (
        "TypeError: 'NoneType' object is not subscriptable\ncellhelp/generate.py:13: in suggest"
    )
    v = rejection((T_INSIDE, "failed", message))
    assert implicated(v) == {
        "cellhelp/generate.py": f"in the output of {T_INSIDE}",
        HANDLER_TEST: f"failing test {T_INSIDE}",
    }
    # a test that failed on its own mock: generate.py's line is only where it called the mock
    mock = (
        "AttributeError: 'types.SimpleNamespace' object has no attribute 'create'\n"
        + (message.split("\n", 1)[1])
    )
    assert implicated(rejection((T_INSIDE, "failed", mock))) == {
        HANDLER_TEST: f"failing test {T_INSIDE}"
    }


def test_a_test_still_failing_widens_to_the_code_under_test():
    v = rejection((T_INSIDE, "failed", "AssertionError: assert 'x' == 'y'"))
    assert list(implicated(v)) == [HANDLER_TEST]
    assert implicated(v, still_failing={T_INSIDE}) == {
        "cellhelp/generate.py": f"imported by {HANDLER_TEST} ({T_INSIDE} still fails)",
        HANDLER_TEST: f"failing test {T_INSIDE}",
    }


def test_a_failing_test_whose_file_is_not_sent_resends_what_it_imports():
    other = "tests.test_other::test_x"
    v = rejection((other, "failed", "AssertionError"))
    assert implicated(v, reaches=lambda test: ["cellhelp/generate.py"]) == {
        "cellhelp/generate.py": (
            f"imported by tests.test_other (failing test {other}; its file is not sent)"
        )
    }


def test_reasons_findings_and_dependency_files():
    finding = Finding(
        "cellhelp/generate.py", 7, 16, 7, 44, "api_call", "openai.ChatCompletion.create", "x"
    )
    tampered = Reason("assertion_removed", "reject", "an assert was removed", (), MAGICS_TEST, 3)
    v = rejection(reasons=[tampered], remaining=[finding])
    assert implicated(v) == {
        "cellhelp/generate.py": "remaining finding",
        MAGICS_TEST: "verifier: assertion_removed",
    }
    # a failed install implicates the dependency files it read, and no Python file
    install = run(status="error", reason="dependency install failed")
    assert implicated(rejection(fixed=install)) == {"requirements.txt": "dependency problem"}


def test_a_rejection_naming_no_file_resends_every_python_file():
    crashed = run(status="error", reason="test phase crashed")
    assert implicated(rejection(fixed=crashed)) == {
        p: "nothing narrower implicated" for p in CANDIDATES[1:]
    }
