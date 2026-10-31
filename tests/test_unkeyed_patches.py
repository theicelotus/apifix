"""A patched resource method while the code under test builds a client without a key
(genai-gptoss-8): the check (code_checks.unkeyed_method_patch_problems), key-setting edits in the
unrelated-edit check, the uncalled-mock failure hint, and the guide's advice."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apifix.engine.code_checks import (
    keyless_client,
    module_roots,
    resource_method,
    unkeyed_method_patch_problems,
)
from apifix.engine.migrations import get_migration
from apifix.engine.models import ClientKey
from apifix.engine.scanner import scan
from apifix.engine.verifier import UNCALLED_MOCK_MARKERS, failure_hints
from apifix.engine.whole_file import check_candidate
from tests.test_code_checks import reader
from tests.test_verifier import run

MIGRATION = get_migration("openai-v1")
KEY = MIGRATION.client_key
OPENAI = frozenset({"openai"})
FIXTURE = Path(__file__).parent / "fixtures" / "keyless_client"
TEST = "tests/test_handler.py"
ORIGINAL_TEST = (FIXTURE / TEST).read_text()
HANDLER = (FIXTURE / "helper" / "handler.py").read_text()
METHOD = "openai.resources.chat.completions.Completions.create"
FINDINGS = [f for f in scan(FIXTURE, "openai-v1").findings if f.file == TEST]

# helper/generate.py migrated as in genai-gptoss-8: a client built inside the function, no key
GENERATE = '''\
"""Suggestions for a notebook cell's error."""

import openai


def suggest(error: str) -> str:
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": f"Explain this error: {error}"}],
    )
    return response.choices[0].message.content
'''
MESSAGE = 'SimpleNamespace(role="assistant", content="Define b")'
REPLY = f"SimpleNamespace(choices=[SimpleNamespace(message={MESSAGE})])"
SURVIVES = """

@mock.patch("builtins.print")
@mock.patch("helper.handler.suggest", side_effect=RuntimeError("boom"))
def test_on_error_survives_a_failure(suggest, print):
    assert on_error("x") == ""
"""
# genai-gptoss-8 attempt 2's shape: the method patched with autospec=True, no key anywhere
RUN8 = f'''\
from types import SimpleNamespace
from unittest import mock

from helper.handler import on_error


@mock.patch(
    "{METHOD}",
    return_value={REPLY},
    autospec=True,
)
def test_on_error(create):
    shown = on_error("NameError: b")
    create.assert_called_once()
    args, kwargs = create.call_args
    assert args == ()
    assert kwargs["model"] == "gpt-3.5-turbo"
    assert shown == "Suggestion: Define b"
{SURVIVES}'''
RUN8_PROBLEM = (
    f"mock target {METHOD!r} patches a method, so the real client is still built: "
    "helper/generate.py:7 calls openai.OpenAI() without api_key=, which raises when "
    "OPENAI_API_KEY is not set (tests run without it, and the code's own try/except may hide "
    "the error), and test_on_error sets no key. Set a placeholder key in the test "
    '(@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}) or '
    'monkeypatch.setenv("OPENAI_API_KEY", "test")), or patch the client class instead, '
    'without autospec: patch("helper.generate.openai.OpenAI") and set '
    "OpenAI.return_value.chat.completions.create.return_value"
)
# the message's first fix: a placeholder key (and no autospec, whose mock gets self as args[0])
KEYED = (
    RUN8.replace("from types", "import os\nfrom types", 1)
    .replace(
        "@mock.patch(\n",
        '@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"})\n@mock.patch(\n',
        1,
    )
    .replace("    autospec=True,\n", "", 1)
)
# the message's second fix: the client class, without autospec
CLIENT_CLASS = f"""\
from types import SimpleNamespace
from unittest import mock

from helper.handler import on_error


@mock.patch("helper.generate.openai.OpenAI")
def test_on_error(OpenAI):
    create = OpenAI.return_value.chat.completions.create
    create.return_value = {REPLY}
    shown = on_error("NameError: b")
    create.assert_called_once()
    args, kwargs = create.call_args
    assert args == ()
    assert kwargs["model"] == "gpt-3.5-turbo"
    assert shown == "Suggestion: Define b"
{SURVIVES}"""


def files(test: str, generate: str = GENERATE, **extra: str) -> dict[str, str]:
    return {
        "helper/__init__.py": "",
        "helper/generate.py": generate,
        "helper/handler.py": HANDLER,
        TEST: test,
        **extra,
    }


def problems(test: str, generate: str = GENERATE, original: str = ORIGINAL_TEST, **extra: str):
    tree = files(test, generate, **extra)
    return unkeyed_method_patch_problems(
        original,
        test,
        reader(tree),
        KEY,
        library_modules=OPENAI,
        roots=module_roots(TEST),
        modules=("helper/__init__.py", "helper/generate.py", "helper/handler.py"),
        test_files=(TEST,),
        path=TEST,
    )


FIXTURE_KEY = (
    '\n\n@pytest.fixture\ndef key(monkeypatch):\n    monkeypatch.setenv("OPENAI_API_KEY", "x")\n'
)


# --- the check --------------------------------------------------------------------------------


def test_run8_shape_is_rejected_with_both_fixes():
    assert problems(RUN8) == [RUN8_PROBLEM]


def test_the_suggested_fixes_pass_the_check():
    assert problems(KEYED) == []
    assert problems(CLIENT_CLASS) == []


@pytest.mark.parametrize(
    ("target", "path"),
    [
        (METHOD, "chat.completions.create"),
        ("openai.resources.chat.Completions.create", "chat.completions.create"),
        ("openai.resources.chat.completions.AsyncCompletions.create", "chat.completions.create"),
        ("openai.resources.Embeddings.create", "embeddings.create"),
        ("openai.resources.fine_tuning.jobs.Jobs.create", "fine_tuning.jobs.create"),
        ("openai.resources.beta.chat.completions.Completions.parse", "beta.chat.completions.parse"),
        ("app.openai.resources.chat.completions.Completions.create", "chat.completions.create"),
        ("openai.Completions.create", None),
        ("openai.resources.chat.completions", None),
        ("openai.OpenAI", None),
        ("app.client.chat.completions.create", None),
        ("other.resources.chat.completions.Completions.create", None),
    ],
)
def test_resource_method_targets(target, path):
    assert resource_method(target, OPENAI, "resources") == path


def test_every_test_without_a_key_is_named():
    two = RUN8 + RUN8.split("\n\n\n", 1)[1].split(SURVIVES)[0].replace(
        "def test_on_error(", "def test_on_error_again("
    )
    (problem,) = problems(two)
    assert "test_on_error sets no key (and likewise in: test_on_error_again)" in problem


@pytest.mark.parametrize(
    "keyed",
    [
        # an environment patch on the test, as a dict or keywords
        lambda t: t.replace(
            "@mock.patch(\n",
            '@mock.patch.dict("os.environ", {"OPENAI_API_KEY": "x"})\n@mock.patch(\n',
            1,
        ),
        lambda t: t.replace(
            "@mock.patch(\n",
            '@mock.patch.dict("os.environ", OPENAI_API_KEY="x")\n@mock.patch(\n',
            1,
        ),
        # in the test's body
        lambda t: t.replace(
            "    shown = on_error(",
            '    with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "x"}):\n'
            "        pass\n    shown = on_error(",
            1,
        ),
        lambda t: t.replace(
            "(create):\n",
            '(create, monkeypatch):\n    monkeypatch.setenv("OPENAI_API_KEY", "x")\n',
            1,
        ),
        lambda t: t.replace(
            "(create):\n", '(create):\n    os.environ["OPENAI_API_KEY"] = "x"\n', 1
        ),
        lambda t: t.replace(
            "(create):\n", '(create):\n    os.environ.setdefault("OPENAI_API_KEY", "x")\n', 1
        ),
        lambda t: t.replace(
            "(create):\n", '(create):\n    os.environ.update({"OPENAI_API_KEY": "x"})\n', 1
        ),
        # a fixture the test uses (directly, through another fixture, by usefixtures)
        lambda t: t.replace("(create):", "(create, key):", 1) + FIXTURE_KEY,
        lambda t: (
            t.replace("(create):", "(create, env):", 1)
            + FIXTURE_KEY
            + "\n\n@pytest.fixture\ndef env(key):\n    yield\n"
        ),
        lambda t: (
            t.replace("@mock.patch(\n", '@pytest.mark.usefixtures("key")\n@mock.patch(\n', 1)
            + (
                '\n\n@pytest.fixture(name="key")\ndef _key(monkeypatch):\n'
                '    monkeypatch.setenv("OPENAI_API_KEY", "x")\n'
            )
        ),
        # an autouse fixture, module-level code
        lambda t: (
            t
            + (
                "\n\n@pytest.fixture(autouse=True)\ndef key(monkeypatch):\n"
                '    monkeypatch.setenv("OPENAI_API_KEY", "x")\n'
            )
        ),
        lambda t: t.replace("\n\n\n", '\n\nos.environ.setdefault("OPENAI_API_KEY", "x")\n\n\n', 1),
        # a key passed to the code under test
        lambda t: t.replace('on_error("NameError: b")', 'on_error("NameError: b", api_key="x")', 1),
    ],
)
def test_a_test_that_sets_a_key_passes(keyed):
    assert problems(keyed(RUN8)) == []


def test_a_key_from_a_class_or_its_setup():
    method = RUN8.split("\n\n\n", 1)[1].split(SURVIVES)[0]
    body = "\n".join("    " + line if line else line for line in method.splitlines())
    body = body.replace("(create):", "(self, create):")
    head = RUN8.split("\n\n\n", 1)[0]
    unkeyed = f"{head}\n\n\nclass TestHandler:\n{body}\n"
    assert problems(unkeyed) == [RUN8_PROBLEM]
    decorated = unkeyed.replace(
        "class TestHandler:",
        '@mock.patch.dict("os.environ", {"OPENAI_API_KEY": "x"})\nclass TestHandler:',
    )
    assert problems(decorated) == []
    setup = unkeyed.replace(
        "class TestHandler:\n",
        "class TestHandler:\n    def setup_method(self):\n"
        '        os.environ["OPENAI_API_KEY"] = "x"\n\n',
    )
    assert problems(setup) == []


def test_a_key_from_conftest_or_pytest_config():
    autouse = "\n\n@pytest.fixture(autouse=True)" + FIXTURE_KEY.split("@pytest.fixture", 1)[1]
    assert problems(RUN8, **{"tests/conftest.py": autouse}) == []
    assert problems(RUN8, **{"conftest.py": autouse}) == []
    module_level = 'import os\n\nos.environ["OPENAI_API_KEY"] = "x"\n'
    assert problems(RUN8, **{"tests/conftest.py": module_level}) == []
    hook = (
        "import os\n\n\ndef pytest_configure(config):\n"
        '    os.environ.setdefault("OPENAI_API_KEY", "x")\n'
    )
    assert problems(RUN8, **{"tests/conftest.py": hook}) == []
    env = '[tool.pytest.ini_options]\nenv = ["OPENAI_API_KEY=x"]\n'
    assert problems(RUN8, **{"pyproject.toml": env}) == []
    # a fixture nobody uses, a conftest in another directory: no key
    unused = autouse.replace("(autouse=True)", "")
    assert problems(RUN8, **{"tests/conftest.py": unused}) == [RUN8_PROBLEM]
    assert problems(RUN8, **{"other/conftest.py": autouse}) == [RUN8_PROBLEM]


def test_a_test_that_also_patches_the_client_class_needs_no_key():
    both = RUN8.replace(
        "@mock.patch(\n", '@mock.patch("helper.generate.openai.OpenAI")\n@mock.patch(\n', 1
    )
    assert problems(both.replace("(create):", "(create, OpenAI):", 1)) == []


@pytest.mark.parametrize(
    "generate",
    [
        GENERATE.replace("openai.OpenAI()", 'openai.OpenAI(api_key="sk-test")'),
        GENERATE.replace("openai.OpenAI()", "openai.OpenAI(**options)"),
        GENERATE.replace("openai.OpenAI()", "openai.AzureOpenAI()"),
        # built at import, before any test runs: not this check's case
        GENERATE.replace("\n\ndef suggest", "\nclient = openai.OpenAI()\n\n\ndef suggest").replace(
            "    client = openai.OpenAI()\n", ""
        ),
        # still on 0.x (no client yet)
        (FIXTURE / "helper" / "generate.py").read_text(),
    ],
)
def test_code_that_builds_no_keyless_client_is_not_flagged(generate):
    assert problems(RUN8, generate) == []


def test_how_the_module_binds_the_client_class_gives_the_target():
    from_import = GENERATE.replace("import openai\n", "from openai import OpenAI\n").replace(
        "openai.OpenAI()", "OpenAI()"
    )
    (problem,) = problems(RUN8, from_import)
    assert "helper/generate.py:7 calls OpenAI() without api_key=" in problem
    assert 'patch("helper.generate.OpenAI") and set OpenAI.return_value.chat' in problem
    aliased = GENERATE.replace("import openai\n", "import openai as oa\n").replace(
        "openai.OpenAI()", "oa.AsyncOpenAI()"
    )
    (problem,) = problems(RUN8, aliased)
    assert 'patch("helper.generate.oa.AsyncOpenAI") and set AsyncOpenAI.return_value' in problem


def test_only_new_or_changed_patches_of_reached_modules_count():
    assert problems(RUN8, original=RUN8) == []
    changed = RUN8.replace("    autospec=True,\n", "", 1)
    assert problems(changed, original=RUN8) == [RUN8_PROBLEM]
    # a test file that reaches no module building a client
    alone = RUN8.replace("from helper.handler import on_error\n", "on_error = print\n")
    assert problems(alone) == []
    # a patch in a fixture applies to no test (not checked)
    fixture = (
        "import pytest\nfrom unittest import mock\n\nfrom helper.handler import on_error\n\n\n"
        "@pytest.fixture\ndef create():\n"
        f'    with mock.patch("{METHOD}") as m:\n        yield m\n\n\n'
        "def test_on_error(create):\n    on_error('x')\n    create.assert_called_once()\n"
    )
    assert problems(fixture) == []


# --- whole-file checks: the run's file, and the key-setting fix is not "unrelated" -----------


def checked(candidate: str, ci_key: bool = False):
    tree = files(candidate)
    return check_candidate(
        TEST,
        ORIGINAL_TEST,
        candidate,
        required=True,
        read_module=reader(tree),
        library_modules=OPENAI,
        module_roots=module_roots(TEST),
        python_modules=("helper/__init__.py", "helper/generate.py", "helper/handler.py"),
        test_files=(TEST,),
        findings=FINDINGS,
        client_classes=MIGRATION.client_classes,
        client_resources=MIGRATION.client_resources,
        client_key=None if ci_key else KEY,
        env_names=(KEY.env,),
    )


def test_whole_file_checks_reject_the_run8_shape_and_accept_the_fixes():
    result = checked(RUN8)
    assert result.errors == [RUN8_PROBLEM]
    assert result.error_checks == ["mock_targets"]
    assert checked(RUN8, ci_key=True).errors == []  # the repo's CI sets the key: a placeholder
    for fix in (KEYED, CLIENT_CLASS):
        assert checked(fix).errors == [], fix
    body = RUN8.replace(
        "(create):\n", '(create, monkeypatch):\n    monkeypatch.setenv("OPENAI_API_KEY", "x")\n', 1
    ).replace("    autospec=True,\n", "", 1)
    assert checked(body).errors == []


def test_key_setting_edits_far_from_findings_need_the_env_names():
    from apifix.engine.unrelated import unrelated_edits

    survives = ORIGINAL_TEST.replace(
        "def test_on_error_survives_a_failure(suggest, print):\n",
        "def test_on_error_survives_a_failure(suggest, print, monkeypatch):\n"
        '    monkeypatch.setenv("OPENAI_API_KEY", "x")\n',
    )
    assert unrelated_edits(ORIGINAL_TEST, survives, FINDINGS, OPENAI, (KEY.env,)) == []
    (error,) = unrelated_edits(ORIGINAL_TEST, survives, FINDINGS, OPENAI)
    assert "unrelated to the migration" in error
    # removing a key setting is still judged as before
    assert unrelated_edits(survives, ORIGINAL_TEST, FINDINGS, OPENAI, (KEY.env,)) != []


# --- in a sandbox-like run: the stub client raises without a key ------------------------------

STUB = {
    "openai/__init__.py": """\
import os
from functools import cached_property

from openai.resources.chat.completions import Completions


class OpenAIError(Exception):
    pass


class Chat:
    @cached_property
    def completions(self):
        return Completions()


class OpenAI:
    def __init__(self, api_key=None, **kwargs):
        if not (api_key or os.environ.get("OPENAI_API_KEY")):
            raise OpenAIError("The api_key client option must be set")

    @cached_property
    def chat(self):
        return Chat()
""",
    "openai/resources/__init__.py": "",
    "openai/resources/chat/__init__.py": "",
    "openai/resources/chat/completions.py": """\
class Completions:
    def create(self, **kwargs):
        raise RuntimeError("no network in tests")
""",
}


def _pytest(root: Path, test: str) -> subprocess.CompletedProcess:
    for rel, text in {**STUB, **files(test)}.items():
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


def test_the_rejected_shape_fails_and_both_fixes_pass(tmp_path):
    rejected = _pytest(tmp_path / "run8", RUN8)
    assert rejected.returncode == 1
    assert "Expected 'create' to have been called once. Called 0 times." in rejected.stdout
    for name, fix in (("keyed", KEYED), ("client", CLIENT_CLASS)):
        result = _pytest(tmp_path / name, fix)
        assert result.returncode == 0, result.stdout
    # the key alone, with autospec kept: the mock gets the Completions object as args[0]
    autospec = KEYED.replace("    return_value=", "    autospec=True,\n    return_value=", 1)
    assert problems(autospec) == []
    result = _pytest(tmp_path / "autospec", autospec)
    assert result.returncode == 1
    assert "Left contains one more item: <openai.resources.chat.completions.Completions" in (
        result.stdout
    )


# --- the failure hint ---------------------------------------------------------------------------


def test_keyless_client_finds_the_first_construction_in_a_function():
    tree = files(RUN8)
    found = keyless_client(
        reader(tree), ["helper/handler.py", "helper/generate.py"], KEY, library_modules=OPENAI
    )
    assert found is not None
    assert (found.rel, found.line, found.call, found.target) == (
        "helper/generate.py",
        7,
        "openai.OpenAI",
        "helper.generate.openai.OpenAI",
    )
    keyed = GENERATE.replace("openai.OpenAI()", 'openai.OpenAI(api_key="x")')
    assert (
        keyless_client(
            reader(files(RUN8, keyed)), ["helper/generate.py"], KEY, library_modules=OPENAI
        )
        is None
    )


def test_uncalled_mock_hint_is_given_once_for_either_marker():
    text = MIGRATION.uncalled_mock_hint.format(
        where="helper/generate.py:7 builds openai.OpenAI()", target="helper.generate.openai.OpenAI"
    )
    assert text.startswith(
        "a mock was never called, and helper/generate.py:7 builds openai.OpenAI()"
    )
    assert "the code's own try/except may swallow the error" in text
    assert '{"OPENAI_API_KEY": "test"}' in text
    assert 'patch("helper.generate.openai.OpenAI"), without autospec' in text
    hints = tuple((m, text) for m in UNCALLED_MOCK_MARKERS)
    called = "AssertionError: Expected 'create' to have been called once. Called 0 times."
    v = SimpleNamespace(fixed=run(("tests/test_handler.py::test_on_error", "failed", called)))
    assert failure_hints(v, hints) == [text]
    both = called + "\nexpected call not found.\nExpected: create()\n  Actual: not called."
    v = SimpleNamespace(fixed=run(("tests/test_handler.py::test_on_error", "failed", both)))
    assert failure_hints(v, hints) == [text]
    other = SimpleNamespace(fixed=run(("tests/test_handler.py::test_on_error", "failed", "boom")))
    assert failure_hints(other, hints) == []


# --- the guide ----------------------------------------------------------------------------------


def test_the_tests_guide_part_says_a_patched_method_needs_a_key():
    tests_part = MIGRATION.guide_text(["tests"])
    assert "Patch the client class (without autospec)" in tests_part
    assert "Patching the method instead leaves the real constructor running" in tests_part
    assert '@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"})' in tests_part
    assert 'monkeypatch.setenv("OPENAI_API_KEY", "test")' in tests_part
    assert "autospec the method instead" not in tests_part
    # the client-class examples come before the method one
    assert tests_part.index('@mock.patch("yourpkg.module.openai.OpenAI")') < tests_part.index(
        '@mock.patch("openai.resources.chat.completions.Completions.create")'
    )
    assert "call_args.args[0]" in tests_part


def test_keyless_client_through_a_dotted_library_module():
    google = frozenset({"google.generativeai", "google.genai"})
    key = ClientKey(env="GOOGLE_API_KEY", keyword="api_key", classes={"Client"}, resources="")
    plain = "import google.genai\n\n\ndef ask():\n    return google.genai.Client()\n"
    other = (
        "import google.cloud.storage\n\n\ndef ask():\n    return google.cloud.storage.Client()\n"
    )
    found = keyless_client(reader({"a.py": plain}), ["a.py"], key, library_modules=google)
    assert found is not None
    assert (found.line, found.call, found.target) == (
        5,
        "google.genai.Client",
        "a.google.genai.Client",
    )
    assert keyless_client(reader({"a.py": other}), ["a.py"], key, library_modules=google) is None
