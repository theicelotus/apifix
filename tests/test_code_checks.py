"""Static checks on a rewritten file: undefined names (pyflakes) and mock targets."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from apifix.engine.code_checks import (
    mock_target_problems,
    mock_targets,
    top_level_names,
    undefined_names,
)
from tests.integration.tiny_repo import ORIGINAL as TINY

TINY_APP = textwrap.dedent(TINY["app.py"])
TINY_TESTS = textwrap.dedent(TINY["tests/test_app.py"])

# qwen2.5-coder-7b's replies in whole-file mode, last attempt of a live run (identical in all
# three attempts): app.py is migrated with the client created inside ask(); the tests use
# SimpleNamespace without importing it and patch app.client, which does not exist.
LIVE_APP = """\
import openai


def ask(question):
    client = openai.OpenAI()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content.strip()
"""

LIVE_TESTS = """\
from unittest import mock

from app import ask


def completion(text):
    return {"choices": [{"message": {"content": text}}]}


@mock.patch("app.client.chat.completions.create")
def test_ask(create):
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))]
    )
    assert ask("hi") == "hello"


@mock.patch("app.client.chat.completions.create")
def test_strips_whitespace(create):
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  hello\\n"))]
    )
    assert ask("hi") == "hello"
"""

LIVE_UNDEFINED = (
    "line 12: 'SimpleNamespace' is used but never imported or defined "
    "(add: from types import SimpleNamespace)"
)
LIVE_TARGET = (
    "mock target 'app.client.chat.completions.create': app.py has no top-level name 'client' "
    "(its top-level names: ask, openai). Patch a name that exists in app.py"
)
OPENAI = frozenset({"openai"})
EXAMPLE = """For example:
    @mock.patch("app.openai.OpenAI")
    def test_ask(OpenAI):
        OpenAI.return_value.chat.completions.create.return_value = <the test's mock response>"""
# The same, with the migrated library known (as in whole-file mode): the concrete fix.
LIVE_TARGET_SUGGESTED = (
    "mock target 'app.client.chat.completions.create': app.py has no top-level name 'client' "
    "(its top-level names: ask, openai); ask() creates it with client = openai.OpenAI(). "
    'Patch the constructor instead: patch("app.openai.OpenAI") and set '
    "OpenAI.return_value.chat.completions.create.return_value.\n" + EXAMPLE
)


# Second live run: the test file accepted in all three attempts (after the check retry). It
# patches ask, the function under test, so the real ask ran and openai.OpenAI() raised.
LIVE2_TESTS = """\
from unittest import mock
from types import SimpleNamespace

from app import ask


def completion(text):
    return {"choices": [{"message": {"content": text}}]}


@mock.patch("app.ask")
def test_ask(mocked_ask):
    mocked_ask.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))]
    )
    assert ask("hi") == "hello"


@mock.patch("app.ask")
def test_strips_whitespace(mocked_ask):
    mocked_ask.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  hello\\n"))]
    )
    assert ask("hi") == "hello"
"""
LIVE2_CODE_UNDER_TEST = (
    "mock target 'app.ask' in test_ask: the test calls ask() from app.py directly, so patching "
    "it replaces the function being tested; patch what it calls instead: ask() creates "
    'client = openai.OpenAI(), so patch("app.openai.OpenAI") and set '
    "OpenAI.return_value.chat.completions.create.return_value.\n" + EXAMPLE
)


def reader(files: dict[str, str]):
    return lambda rel: files.get(rel)


# --- the live run -------------------------------------------------------------------------------


def test_live_run_undefined_simple_namespace():
    assert undefined_names("tests/test_app.py", TINY_TESTS, LIVE_TESTS) == [LIVE_UNDEFINED]


def test_live_run_mock_target_missing_from_app():
    problems = mock_target_problems(TINY_TESTS, LIVE_TESTS, reader({"app.py": LIVE_APP}))
    assert problems == [LIVE_TARGET]  # reported once, though patched twice


def test_live_run_tests_pass_once_fixed():
    fixed = "from types import SimpleNamespace\n" + LIVE_TESTS
    assert undefined_names("tests/test_app.py", TINY_TESTS, fixed) == []
    # patching the class the client comes from, or a module-level client, is fine
    via_class = LIVE_TESTS.replace("app.client.chat.completions.create", "app.openai.OpenAI")
    assert mock_target_problems(TINY_TESTS, via_class, reader({"app.py": LIVE_APP})) == []
    module_client = LIVE_APP.replace(
        "\n\ndef ask(question):\n    client = openai.OpenAI()\n",
        "\nclient = openai.OpenAI()\n\n\ndef ask(question):\n",
    )
    assert mock_target_problems(TINY_TESTS, LIVE_TESTS, reader({"app.py": module_client})) == []


# --- undefined names ----------------------------------------------------------------------------


def test_names_already_undefined_in_the_original_are_not_reported():
    original = "def f():\n    return helper(1)\n"
    candidate = "def f():\n    return helper(2) + other(3)\n"
    assert undefined_names("m.py", original, candidate) == [
        "line 2: 'other' is used but never imported or defined"
    ]


def test_star_imports_and_builtins_are_not_reported():
    original = "from os.path import *\n\nx = join('a', 'b')\n"
    candidate = "from os.path import *\n\nx = join('a', basename('b'))\nprint(len(__file__))\n"
    assert undefined_names("m.py", original, candidate) == []


def test_function_level_definition_does_not_define_a_name_elsewhere():
    original = "def make():\n    client = 1\n    return client\n"
    candidate = original + "\n\ndef use():\n    return client\n"
    assert undefined_names("m.py", original, candidate) == [
        "line 7: 'client' is used but never imported or defined"
    ]
    module_level = "client = 1\n\n\ndef use():\n    return client\n"
    assert undefined_names("m.py", original, module_level) == []


@pytest.mark.parametrize(
    ("name", "suggestion"),
    [
        ("MagicMock", "from unittest.mock import MagicMock"),
        ("patch", "from unittest.mock import patch"),
        ("Mock", "from unittest.mock import Mock"),
        ("dataclass", "from dataclasses import dataclass"),
        ("Path", "from pathlib import Path"),
        ("json", "import json"),
    ],
)
def test_common_stdlib_names_get_an_import_suggestion(name, suggestion):
    (problem,) = undefined_names("m.py", "x = 1\n", f"x = {name}\n")
    assert problem == (
        f"line 1: {name!r} is used but never imported or defined (add: {suggestion})"
    )


def test_each_name_is_reported_once_at_its_first_use_and_in_line_order():
    candidate = "a = zeta\nb = alpha\nc = zeta\n"
    assert undefined_names("m.py", "", candidate) == [
        "line 1: 'zeta' is used but never imported or defined",
        "line 2: 'alpha' is used but never imported or defined",
    ]


def test_unparsable_versions_report_nothing():
    assert undefined_names("m.py", "x = 1\n", "def f(:\n") == []
    assert undefined_names("m.py", "print 'py2'\n", "x = y\n") == []


# --- mock targets: which calls ------------------------------------------------------------------


def test_patch_call_forms_are_found_and_object_dict_ignored():
    source = textwrap.dedent(
        """\
        import unittest.mock
        from unittest import mock
        from unittest.mock import patch

        @patch("a.one")
        @mock.patch("a.two")
        @unittest.mock.patch("a.three", new=1)
        def test_x(mocker):
            mocker.patch("a.four")
            with patch("a.five"):
                pass
            patch.object(obj, "six")
            patch.dict("os.environ", {})
            mock.patch.object(obj, "seven")
            other.patch("a.eight")
            patch(target_variable)
        """
    )
    assert mock_targets(source) == [
        (5, "a.one"),
        (6, "a.two"),
        (7, "a.three"),
        (9, "a.four"),
        (10, "a.five"),
    ]


# --- mock targets: resolution -------------------------------------------------------------------


def test_unchanged_targets_are_not_checked():
    original = '@mock.patch("app.gone")\ndef test_a(m):\n    pass\n'
    candidate = original + '\n@mock.patch("app.gone_too")\ndef test_b(m):\n    pass\n'
    problems = mock_target_problems(original, candidate, reader({"app.py": "x = 1\n"}))
    assert [p.split(":")[0] for p in problems] == ["mock target 'app.gone_too'"]


def test_targets_outside_the_repo_are_not_checked():
    candidate = '@mock.patch("openai.resources.chat.Completions.create")\ndef t(m):\n    pass\n'
    assert mock_target_problems("", candidate, reader({"app.py": "x = 1\n"})) == []


@pytest.mark.parametrize("module_path", ["pkg/mod.py", "pkg/mod/__init__.py", "src/pkg/mod.py"])
def test_dotted_module_resolution(module_path):
    files = {"pkg/__init__.py": "", module_path: "import os\n\n\ndef run():\n    pass\n"}
    ok = 'patch("pkg.mod.run")\npatch("pkg.mod.os.system")\n'
    assert mock_target_problems("", ok, reader(files)) == []
    bad = 'patch("pkg.mod.client.create")\n'
    assert mock_target_problems("", bad, reader(files)) == [
        f"mock target 'pkg.mod.client.create': {module_path} has no top-level name 'client' "
        f"(its top-level names: os, run). Patch a name that exists in {module_path}"
    ]


def test_a_submodule_counts_as_a_name_of_its_package():
    files = {"pkg/__init__.py": "VERSION = 1\n", "pkg/mod.py": "x = 1\n", "pkg/sub/__init__.py": ""}
    assert mock_target_problems("", 'patch("pkg.mod")\npatch("pkg.sub")\n', reader(files)) == []
    (problem,) = mock_target_problems("", 'patch("pkg.nope")\n', reader(files))
    assert "pkg/__init__.py has no top-level name 'nope' (its top-level names: VERSION)" in problem


def test_module_level_definitions_of_every_kind_count():
    module = textwrap.dedent(
        """\
        import os.path
        import json as j
        from typing import Any as A
        A1 = A2 = 1
        B1, (B2, *B3) = 1, (2, 3)
        C: int = 1
        D = 0
        D += 1
        try:
            import fast as E
        except ImportError:
            E = None
        if True:
            F = 1
        else:
            G = 2
        with open("x") as H:
            pass
        for I in range(1):
            pass
        class K: ...
        async def L(): ...
        def setup():
            global M
            M = 1
            local = 2
        """
    )
    names = top_level_names(module)
    expected = {"os", "j", "A", "A1", "A2", "B1", "B2", "B3", "C", "D", "E", "F", "G", "H"}
    assert names == expected | {"I", "K", "L", "M", "setup"}
    assert "local" not in names


def test_modules_whose_names_cannot_be_known_are_not_checked():
    target = 'patch("m.anything")\n'
    for module in ("from os import *\n", "def __getattr__(name):\n    return 1\n", "def f(:\n"):
        assert mock_target_problems("", target, reader({"m.py": module})) == []


def test_many_names_are_listed_up_to_a_limit():
    module = "".join(f"n{i:02} = {i}\n" for i in range(25))
    (problem,) = mock_target_problems("", 'patch("m.missing")\n', reader({"m.py": module}))
    assert "n00, n01," in problem and "n19, ..." in problem and "n20" not in problem


def test_unparsable_candidate_has_no_targets():
    assert mock_targets('patch("a.b"\n') == []


# --- the second live run: patching the code under test --------------------------------------------


def test_second_live_run_patching_the_function_under_test_is_rejected():
    problems = mock_target_problems(
        TINY_TESTS, LIVE2_TESTS, reader({"app.py": LIVE_APP}), library_modules=OPENAI
    )
    assert problems == [LIVE2_CODE_UNDER_TEST]  # once, though both tests patch it
    # without the library, the same rule and the generic advice
    (plain,) = mock_target_problems(TINY_TESTS, LIVE2_TESTS, reader({"app.py": LIVE_APP}))
    assert plain.endswith("replaces the function being tested; patch what it calls instead")


def test_first_live_run_target_gets_the_concrete_fix():
    problems = mock_target_problems(
        TINY_TESTS, LIVE_TESTS, reader({"app.py": LIVE_APP}), library_modules=OPENAI
    )
    assert problems == [LIVE_TARGET_SUGGESTED]


SUGGESTED_FIX = LIVE2_TESTS.replace('"app.ask"', '"app.openai.OpenAI"').replace(
    "mocked_ask.return_value", "mocked_ask.return_value.chat.completions.create.return_value"
)
STUB_OPENAI = """\
class OpenAIError(Exception):
    pass


class OpenAI:
    def __init__(self, **kwargs):
        raise OpenAIError("The api_key client option must be set")
"""


def _pytest(root, tests: str) -> subprocess.CompletedProcess:
    """Run `tests` against the live app.py with an openai stub that, like the real one,
    raises without an API key."""
    root.mkdir()
    for rel, text in {"app.py": LIVE_APP, "openai.py": STUB_OPENAI, "test_app.py": tests}.items():
        (root / rel).write_text(text)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_app.py"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_suggested_fix_passes_the_check_and_the_tests(tmp_path):
    files = reader({"app.py": LIVE_APP})
    assert mock_target_problems(TINY_TESTS, SUGGESTED_FIX, files, library_modules=OPENAI) == []
    live = _pytest(tmp_path / "live", LIVE2_TESTS)
    assert live.returncode == 1 and "api_key client option must be set" in live.stdout
    fixed = _pytest(tmp_path / "fixed", SUGGESTED_FIX)
    assert fixed.returncode == 0, fixed.stdout


# --- code under test: per test ---------------------------------------------------------------


def _problems(tests: str, app: str = LIVE_APP, original: str = "") -> list[str]:
    files = reader({"app.py": textwrap.dedent(app)})
    return mock_target_problems(original, textwrap.dedent(tests), files, library_modules=OPENAI)


def test_aliased_import_and_with_statement_are_rejected():
    tests = """\
        from unittest import mock
        from app import ask as do_ask

        def test_a():
            with mock.patch("app.ask"):
                assert do_ask("q")
    """
    (problem,) = _problems(tests)
    assert problem.startswith("mock target 'app.ask' in test_a: the test calls do_ask() from")


def test_module_call_is_rejected():
    tests = """\
        import app
        from unittest.mock import patch

        @patch("app.ask")
        def test_a(m):
            app.ask("q")
    """
    (problem,) = _problems(tests)
    assert "the test calls app.ask() from app.py directly" in problem
    assert '    @patch("app.openai.OpenAI")\n    def test_a(OpenAI):' in problem


def test_mocker_patch_gets_a_mocker_example():
    tests = """\
        from app import ask

        def test_a(mocker):
            mocker.patch("app.ask")
            ask("q")
    """
    (problem,) = _problems(tests)
    assert problem.endswith(
        'For example:\n    OpenAI = mocker.patch("app.openai.OpenAI")\n'
        "    OpenAI.return_value.chat.completions.create.return_value = <the test's mock response>"
    )


def test_patching_a_function_the_test_does_not_call_is_fine():
    app = LIVE_APP + "\n\ndef main():\n    return ask('q')\n"
    tests = """\
        from unittest import mock
        from app import ask, main

        @mock.patch("app.ask")
        def test_main(m):
            m.return_value = "x"
            assert main() == "x"

        def test_ask():
            assert ask
    """
    assert _problems(tests, app) == []  # per test: test_main never calls ask() itself


def test_class_decorator_applies_to_its_test_methods():
    tests = """\
        from unittest import mock, TestCase
        from app import ask

        @mock.patch("app.ask")
        class T(TestCase):
            def test_a(self, m):
                ask("q")
    """
    (problem,) = _problems(tests)
    assert "in test_a:" in problem


def test_fixtures_helpers_and_module_level_patches_are_not_checked():
    tests = """\
        import pytest
        from unittest import mock
        from app import ask

        patcher = mock.patch("app.ask")

        @pytest.fixture
        def fake(mocker):
            mocker.patch("app.ask")

        def helper():
            with mock.patch("app.ask"):
                ask("q")

        def test_a(fake):
            ask("q")
    """
    assert _problems(tests) == []


BOT = """\
    class Bot:
        def helper(self):
            pass

        def run(self):
            return self.helper()
"""
BOT_TESTS = """\
    from unittest import mock
    from app import Bot

    @mock.patch("app.Bot.helper")
    def test_run(m):
        Bot().run()
"""


def test_a_method_of_the_class_under_test_may_be_patched():
    assert _problems(BOT_TESTS, BOT) == []
    (problem,) = _problems(BOT_TESTS.replace("app.Bot.helper", "app.Bot"), BOT)
    assert "the test calls Bot() from app.py directly" in problem
    assert problem.endswith("patch what it calls instead")  # no library call to suggest


def test_unchanged_targets_and_submodules_are_not_code_under_test():
    assert _problems(LIVE2_TESTS, original=LIVE2_TESTS) == []  # already in the original
    files = {"pkg/__init__.py": "", "pkg/mod.py": "def f():\n    pass\n"}
    sub = """\
        from pkg import mod
        from unittest import mock

        @mock.patch("pkg.mod")
        def test_a(m):
            mod()
    """
    sub = textwrap.dedent(sub)
    assert mock_target_problems("", sub, reader(files), library_modules=OPENAI) == []


# --- suggestions ------------------------------------------------------------------------------


def _suggestion(app: str, target: str = "app.ask") -> str:
    tests = f"""\
        from unittest import mock
        from app import ask

        @mock.patch("{target}")
        def test_a(m):
            ask("q")
    """
    (problem,) = _problems(tests, app)
    return problem


def test_suggestion_for_from_import():
    app = LIVE_APP.replace("import openai\n", "from openai import OpenAI\n").replace(
        "openai.OpenAI()", "OpenAI()"
    )
    problem = _suggestion(app)
    assert 'so patch("app.OpenAI") and set OpenAI.return_value.chat.completions.create' in problem
    missing = _suggestion(app, "app.client.chat.completions.create")
    assert "ask() creates it with client = OpenAI(). Patch the constructor instead: " in missing
    assert 'patch("app.OpenAI")' in missing


def test_suggestion_for_a_module_level_client():
    app = """\
        import openai

        client = openai.OpenAI()


        def ask(q):
            return client.chat.completions.create(model="m").choices[0]
    """
    problem = _suggestion(app)
    assert "ask() calls the module-level client, so " in problem
    assert 'patch("app.client.chat.completions.create") and set create.return_value.' in problem
    assert "    def test_a(create):\n        create.return_value = " in problem


LAZY = """\
    import functools
    import openai


    @functools.lru_cache
    def get_client():
        return openai.OpenAI()


    def ask(q):
"""


@pytest.mark.parametrize(
    "body",
    [
        "    return get_client().chat.completions.create(model='m')\n",
        "    client = get_client()\n    return client.chat.completions.create(model='m')\n",
    ],
)
def test_suggestion_for_a_lazily_built_client(body):
    app = textwrap.dedent(LAZY) + body
    assert (
        'patch("app.get_client") and set get_client.return_value.chat.completions.create'
        ".return_value" in _suggestion(app)
    )


def test_suggestion_for_a_client_from_a_factory_when_the_name_is_missing():
    app = textwrap.dedent(LAZY) + (
        "    client = get_client()\n    return client.chat.completions.create(model='m')\n"
    )
    missing = _suggestion(app, "app.client.chat.completions.create")
    assert "ask() gets it from client = get_client(). Patch that instead: " in missing
    assert 'patch("app.get_client") and set get_client.return_value.chat.completions' in missing


def test_suggestion_for_a_direct_library_call():
    app = """\
        import json
        import openai


        def ask(q):
            json.dumps(q)
            return openai.moderations.create(input=q)
    """
    problem = _suggestion(app)
    assert "ask() calls openai.moderations.create(), so " in problem
    assert 'patch("app.openai.moderations.create") and set create.return_value.' in problem


def test_no_suggestion_without_a_library_call():
    app = "import json\n\n\ndef ask(q):\n    return json.dumps(q.strip())\n"
    assert _suggestion(app).endswith("patch what it calls instead")
    tests = """\
        from unittest import mock

        @mock.patch("app.client.x")
        def test_a(m):
            pass
    """
    assert _problems(tests, app) == [
        "mock target 'app.client.x': app.py has no top-level name 'client' (its top-level names: "
        "ask, json). Patch a name that exists in app.py"
    ]
