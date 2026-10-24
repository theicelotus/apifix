"""Static checks on a rewritten file: undefined names (pyflakes) and mock targets."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from apifix.engine.code_checks import (
    STDLIB_IMPORTS,
    add_missing_imports,
    import_table,
    mock_target_problems,
    mock_targets,
    module_roots,
    top_level_names,
    undefined_names,
)
from apifix.engine.migrations import get_migration
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

# Whole-file mode passes the migration's response shapes: the example sets an object, and the
# message names the dict-returning helper.
WF_FIX = (
    'patch("app.openai.OpenAI") and set OpenAI.return_value.chat.completions.create'
    ".return_value. Mock responses must be objects, not dicts: completion() returns a dict; "
    "make it return an object, e.g. with SimpleNamespace.\n"
    "For example:\n"
    '    @mock.patch("app.openai.OpenAI")\n'
    "    def test_ask(OpenAI):\n"
    "        OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(\n"
    '            choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", '
    'content="hello"))]\n'
    "        )"
)
LIVE_TARGET_WF = (
    "mock target 'app.client.chat.completions.create': app.py has no top-level name 'client' "
    "(its top-level names: ask, openai); ask() creates it with client = openai.OpenAI(). "
    "Patch the constructor instead: " + WF_FIX
)
LIVE2_CODE_UNDER_TEST_WF = (
    "mock target 'app.ask' in test_ask: the test calls ask() from app.py directly, so patching "
    "it replaces the function being tested; patch what it calls instead: ask() creates "
    "client = openai.OpenAI(), so " + WF_FIX
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


def _pytest(root, tests: str, app: str = LIVE_APP) -> subprocess.CompletedProcess:
    """Run `tests` against a live app.py with an openai stub that, like the real one, raises
    without an API key."""
    root.mkdir()
    for rel, text in {"app.py": app, "openai.py": STUB_OPENAI, "test_app.py": tests}.items():
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


def test_suggestion_for_the_module_level_client():
    """Code that keeps openai.api_key and calls the module-level client (client.config)."""
    app = """\
        import openai


        def ask(q):
            r = openai.chat.completions.create(model="m", messages=[q])
            return r.choices[0].message.content
    """
    problem = _suggestion(app)
    assert "ask() calls openai.chat.completions.create(), so " in problem
    assert 'patch("app.openai.chat.completions.create") and set create.return_value.' in problem


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


# --- live run 3: automatic imports, object-shaped mock data ------------------------------------

RUN3 = Path(__file__).parent / "fixtures" / "live_run3"
RUN3_APP_REPLIES = [(RUN3 / f"app_reply_{i}.txt").read_text() for i in range(1, 6)]
RUN3_APP = RUN3_APP_REPLIES[-1]  # attempt 3's app.py, accepted
RUN3_TESTS_1, RUN3_TESTS_2 = ((RUN3 / f"tests_reply_{i}.txt").read_text() for i in (1, 2))
KNOWN = import_table(STDLIB_IMPORTS, get_migration("openai-v1").known_imports)


@pytest.mark.parametrize(
    ("reply", "added"),
    [
        (RUN3_APP_REPLIES[0], ["import os"]),
        (RUN3_APP_REPLIES[1], ["from openai import OpenAI"]),
        (RUN3_APP_REPLIES[2], ["from openai import OpenAI"]),
        (RUN3_APP_REPLIES[3], ["import os"]),
        (RUN3_APP_REPLIES[4], []),
    ],
)
def test_run3_oscillating_app_replies_are_completed(reply, added):
    text, got = add_missing_imports("app.py", TINY_APP, reply, KNOWN)
    assert got == added
    assert undefined_names("app.py", TINY_APP, text, KNOWN) == []


def test_auto_import_cases():
    original = "import openai\n"
    table = import_table(STDLIB_IMPORTS, {"OpenAI": "from openai import OpenAI"})
    stdlib = "import openai\n\nx = SimpleNamespace(a=os.sep)\n"
    assert add_missing_imports("m.py", original, stdlib, table)[1] == [
        "from types import SimpleNamespace",
        "import os",
    ]
    migration = "import openai\n\nclient = OpenAI()\n"
    assert add_missing_imports("m.py", original, migration, table) == (
        "import openai\nfrom openai import OpenAI\n\nclient = OpenAI()\n",
        ["from openai import OpenAI"],
    )
    ambiguous = import_table(
        {"Client": "from openai import OpenAI as Client"}, {"Client": "from httpx import Client"}
    )
    assert ambiguous == {
        "Client": ("from openai import OpenAI as Client", "from httpx import Client")
    }
    code = "import openai\n\nc = Client()\n"
    assert add_missing_imports("m.py", original, code, ambiguous) == (code, [])
    assert undefined_names("m.py", original, code, ambiguous) == [
        "line 3: 'Client' is used but never imported or defined (add one of: from openai "
        "import OpenAI as Client; from httpx import Client)"
    ]
    unknown = "import openai\n\nc = Whatever()\n"
    assert add_missing_imports("m.py", original, unknown, table) == (unknown, [])
    assert undefined_names("m.py", original, unknown, table) == [
        "line 3: 'Whatever' is used but never imported or defined"
    ]


def test_names_undefined_in_the_original_get_no_import():
    original = "x = os.sep\n"  # relies on something else (e.g. exec'd globals)
    assert add_missing_imports("m.py", original, "x = os.sep\ny = 1\n", KNOWN) == (
        "x = os.sep\ny = 1\n",
        [],
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # after the leading imports, not after later ones
        (
            "import a\nfrom b import (\n    c,\n)\n\nx = os.sep\nimport d\n",
            "import a\nfrom b import (\n    c,\n)\nimport os\n\nx = os.sep\nimport d\n",
        ),
        # after a docstring, with no imports
        ('"""Doc."""\n\nx = os.sep\n', '"""Doc."""\nimport os\n\nx = os.sep\n'),
        # nothing at all before the code
        ("x = os.sep\n", "import os\nx = os.sep\n"),
        # CRLF line endings are kept, and a last import line without a newline
        ("import a\r\n\r\nx = os.sep\r\n", "import a\r\nimport os\r\n\r\nx = os.sep\r\n"),
        ("import a; x = os.sep", "import a; x = os.sep\nimport os\n"),
    ],
)
def test_where_imports_are_inserted(source, expected):
    text, added = add_missing_imports("m.py", "", source, KNOWN)
    assert (text, added) == (expected, ["import os"])
    ast.parse(text)


MIGRATION = get_migration("openai-v1")


def _wf_problems(tests: str, app: str) -> list[str]:
    return mock_target_problems(
        TINY_TESTS,
        tests,
        reader({"app.py": app}),
        library_modules=OPENAI,
        mock_responses=MIGRATION.mock_responses,
    )


def test_run3_test_file_gets_the_object_shaped_example():
    (problem,) = _wf_problems(RUN3_TESTS_1, RUN3_APP)
    assert problem == (
        "mock target 'app.client.chat.completions.create': app.py has no top-level name 'client' "
        "(its top-level names: OpenAI, ask, os); ask() creates it with client = "
        "OpenAI(api_key=os.getenv('OPENAI_API_KEY')). Patch the constructor instead: "
        'patch("app.OpenAI") and set OpenAI.return_value.chat.completions.create.return_value. '
        "Mock responses must be objects, not dicts: completion() returns a dict; make it return "
        "an object, e.g. with SimpleNamespace.\n"
        "For example:\n"
        '    @mock.patch("app.OpenAI")\n'
        "    def test_ask(OpenAI):\n"
        "        OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(\n"
        '            choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", '
        'content="hello"))]\n'
        "        )"
    )
    assert _wf_problems(RUN3_TESTS_2, RUN3_APP) == []  # the target was right in the end


RUN3_FIXED_TESTS = RUN3_TESTS_2.replace(
    'return {"choices": [{"message": {"content": text}}]}',
    "return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])",
).replace(
    "from unittest import mock\n", "from types import SimpleNamespace\nfrom unittest import mock\n"
)


def test_run3_dict_helper_fails_and_the_object_helper_passes(tmp_path):
    live = _pytest(tmp_path / "live", RUN3_TESTS_2, app=RUN3_APP)
    assert live.returncode == 1 and "'dict' object has no attribute 'choices'" in live.stdout
    fixed = _pytest(tmp_path / "fixed", RUN3_FIXED_TESTS, app=RUN3_APP)
    assert fixed.returncode == 0, fixed.stdout


def test_example_responses_follow_the_call_and_the_helpers():
    app = "import openai\n\n\ndef ask(q):\n    return openai.embeddings.create(input=q)\n"
    tests = (
        "from unittest import mock\nfrom app import ask\n\n\ndef r1():\n    return {}\n\n\n"
        "def r2():\n    return {'a': 1}\n\n\n@mock.patch('app.ask')\ndef test_a(m):\n    ask('q')\n"
    )
    (problem,) = _wf_problems(tests, app)
    assert "r1(), r2() return dicts; make them return objects" in problem
    assert (
        "create.return_value = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])"
        in (problem)
    )  # short enough for one line
    no_helpers = tests.replace("return {}", "return 1").replace("return {'a': 1}", "return 2")
    (problem,) = _wf_problems(no_helpers, app)
    assert "Mock responses must be objects, not dicts (1.x code uses attribute access)." in problem
    # a call with no known shape keeps the placeholder, and no note
    unknown = app.replace("embeddings.create", "batches.create")
    (problem,) = _wf_problems(no_helpers, unknown)
    assert "= <the test's mock response>" in problem and "Mock responses" not in problem


# --- module roots: the test's own directory and the test plan's import paths -----------------

SVC_SERVER = """\
import openai


def reply(text):
    client = openai.OpenAI()
    r = client.chat.completions.create(model="m", messages=[{"role": "user", "content": text}])
    return r.choices[0].message.content
"""
SVC_TEST_OLD = """\
import unittest
from unittest.mock import patch

from server import reply


class ServerTest(unittest.TestCase):
    @patch("server.openai.ChatCompletion.create")
    def test_reply(self, create):
        create.return_value = {"choices": [{"message": {"content": "hi"}}]}
        self.assertEqual(reply("q"), "hi")
"""


def test_module_roots():
    assert module_roots("svc/testserver.py", ("svc",)) == ("svc/", "", "src/")
    assert module_roots("tests/unit/test_a.py", ("lib",)) == ("tests/unit/", "lib/", "", "src/")
    assert module_roots("test_a.py") == ("", "src/")


def test_mock_targets_resolve_through_the_tests_directory():
    files = reader({"svc/server.py": SVC_SERVER})
    missing = SVC_TEST_OLD.replace("server.openai.ChatCompletion.create", "server.client.create")
    roots = module_roots("svc/testserver.py", ("svc",))
    (problem,) = mock_target_problems(
        SVC_TEST_OLD, missing, files, roots=roots, library_modules=frozenset({"openai"})
    )
    assert "svc/server.py has no top-level name 'client'" in problem
    assert "server.openai.OpenAI" in problem  # the suggestion found the module's client
    # without the roots, `server` is not a repo module and nothing is checked
    assert mock_target_problems(SVC_TEST_OLD, missing, files) == []
    good = SVC_TEST_OLD.replace("server.openai.ChatCompletion.create", "server.openai.OpenAI")
    assert mock_target_problems(SVC_TEST_OLD, good, files, roots=roots) == []


def test_code_under_test_is_found_through_an_import_path():
    files = reader({"svc/server.py": SVC_SERVER})
    patched_reply = SVC_TEST_OLD.replace("server.openai.ChatCompletion.create", "server.reply")
    roots = module_roots("tests/test_server.py", ("svc",))
    (problem,) = mock_target_problems(SVC_TEST_OLD, patched_reply, files, roots=roots)
    assert "server.reply" in problem


# --- patch where it is used: library names bound with `from openai import X` -----------------

LOOKALIKE = Path(__file__).parent / "fixtures" / "sdk_lookalike"
LOOKALIKE_MODULES = (
    "src/__init__.py",
    "src/storyteller/__init__.py",
    "src/storyteller/llm.py",
    "src/storyteller/scoring.py",
)
LOOKALIKE_TESTS = ("conftest.py", "tests/test_llm.py", "tests/test_scoring.py")
LLM_FROM = (
    (LOOKALIKE / "src/storyteller/llm.py")
    .read_text()
    .replace("import openai\n", "from openai import OpenAI\n")
    .replace("openai.ChatCompletion.create(", "OpenAI().chat.completions.create(")
)
SCORING_FROM = (
    (LOOKALIKE / "src/storyteller/scoring.py")
    .read_text()
    .replace("import openai\n", "from openai import OpenAI\n")
    .replace("openai.ChatCompletion.create(", "OpenAI().chat.completions.create(")
)
SCORING_MODULE = (
    (LOOKALIKE / "src/storyteller/scoring.py")
    .read_text()
    .replace("openai.ChatCompletion.create(", "openai.OpenAI().chat.completions.create(")
)
CONFTEST = (LOOKALIKE / "conftest.py").read_text()
PATCHES_OPENAI = CONFTEST.replace(
    """    with patch("openai.ChatCompletion.create") as mock_create:
        mock_create.return_value = {"choices": [{"message": {"content": canned_reply}}]}
        yield mock_create
""",
    """    mock_client = MagicMock()
    with patch("openai.OpenAI", return_value=mock_client):
        yield mock_client
""",
)
assert PATCHES_OPENAI != CONFTEST


def working_copy(**changed: str):
    """The fixture's files, with `changed` ({"llm": text, ...} for src/storyteller/<name>.py,
    "conftest" for conftest.py) standing in."""
    paths = {"conftest": "conftest.py", "llm": "src/storyteller/llm.py"}
    paths["scoring"] = "src/storyteller/scoring.py"
    files = {paths[k]: v for k, v in changed.items()}

    def read(rel: str) -> str | None:
        if rel in files:
            return files[rel]
        f = LOOKALIKE / rel
        return f.read_text() if f.is_file() else None

    return read


def binding_problems(candidate: str, **changed: str) -> list[str]:
    return mock_target_problems(
        CONFTEST,
        candidate,
        working_copy(conftest=candidate, **changed),
        library_modules=frozenset({"openai"}),
        roots=module_roots("conftest.py"),
        modules=LOOKALIKE_MODULES,
        test_files=LOOKALIKE_TESTS,
    )


def test_a_library_patch_misses_modules_that_import_the_name_from_it():
    assert binding_problems(PATCHES_OPENAI, llm=LLM_FROM, scoring=SCORING_FROM) == [
        'patch("openai.OpenAI") does not affect src/storyteller/llm.py, which imports OpenAI '
        'with "from openai import OpenAI"; patch "src.storyteller.llm.OpenAI" instead (and '
        "likewise for: src.storyteller.scoring)"
    ]
    # a module that calls openai.OpenAI() through `import openai` is reached by the patch
    assert binding_problems(PATCHES_OPENAI, llm=LLM_FROM, scoring=SCORING_MODULE) == [
        'patch("openai.OpenAI") does not affect src/storyteller/llm.py, which imports OpenAI '
        'with "from openai import OpenAI"; patch "src.storyteller.llm.OpenAI" instead'
    ]
    llm_module = LLM_FROM.replace("from openai import OpenAI\n", "import openai\n").replace(
        "OpenAI()", "openai.OpenAI()"
    )
    assert binding_problems(PATCHES_OPENAI, llm=llm_module, scoring=SCORING_MODULE) == []
    # modules not migrated yet (the working copy still has `import openai`) are not flagged
    assert binding_problems(PATCHES_OPENAI) == []


def test_patching_each_module_by_the_path_the_tests_import_it_by_passes():
    both = PATCHES_OPENAI.replace(
        '    with patch("openai.OpenAI", return_value=mock_client):\n',
        '    with patch("openai.OpenAI", return_value=mock_client), patch(\n'
        '        "src.storyteller.llm.OpenAI", return_value=mock_client\n'
        '    ), patch("src.storyteller.scoring.OpenAI", return_value=mock_client):\n',
    )
    assert binding_problems(both, llm=LLM_FROM, scoring=SCORING_FROM) == []
    # by a path the tests do not use (src/ is not on sys.path here), it is still missed
    other = both.replace('"src.storyteller.llm.OpenAI"', '"storyteller.llm.OpenAI"')
    assert binding_problems(other, llm=LLM_FROM, scoring=SCORING_FROM) == [
        'patch("openai.OpenAI") does not affect src/storyteller/llm.py, which imports OpenAI '
        'with "from openai import OpenAI"; patch "src.storyteller.llm.OpenAI" instead '
        '("storyteller.llm.OpenAI" names it by a path the tests do not import it by)'
    ]


def test_targets_the_original_already_patched_are_not_checked():
    assert (
        mock_target_problems(
            PATCHES_OPENAI,
            PATCHES_OPENAI,
            working_copy(llm=LLM_FROM),
            library_modules=frozenset({"openai"}),
            modules=LOOKALIKE_MODULES,
            test_files=LOOKALIKE_TESTS,
        )
        == []
    )


APP_FROM = "from openai import OpenAI\n\n\ndef ask():\n    return OpenAI()\n"
TEST_PATCH = (
    'from unittest import mock\n\n\n@mock.patch("openai.OpenAI")\ndef test_ask(m):\n    pass\n'
)


def app_problems(files: dict[str, str], test: str = TEST_PATCH) -> list[str]:
    files = {"tests/test_app.py": test, **files}
    return mock_target_problems(
        "",
        test,
        reader(files),
        library_modules=frozenset({"openai"}),
        roots=module_roots("tests/test_app.py"),
        modules=tuple(f for f in files if not f.startswith("tests/")),
        test_files=tuple(f for f in files if f.startswith("tests/")),
    )


@pytest.mark.parametrize(
    ("files", "target"),
    [
        # no file imports it: the shortest of its paths
        ({"src/app/api.py": APP_FROM}, "app.api.OpenAI"),
        # another module imports it as src.app.api
        ({"src/app/api.py": APP_FROM, "src/app/cli.py": "import src.app.api\n"}, "src.app.api"),
        # the tests import it as app.api, though another module uses src.app.api
        (
            {
                "src/app/api.py": APP_FROM,
                "src/app/cli.py": "from src.app import api\n",
                "tests/test_other.py": "from app.api import ask\n",
            },
            "app.api.OpenAI",
        ),
        # a package's __init__.py
        ({"app/__init__.py": APP_FROM}, "app.OpenAI"),
    ],
)
def test_the_suggested_target_uses_the_path_the_tests_import_it_by(files, target):
    (problem,) = app_problems(files)
    assert f'patch "{target}' in problem


@pytest.mark.parametrize(
    "app",
    [
        # imported inside a function: runs at call time, under the patch
        "def ask():\n    from openai import OpenAI\n\n    return OpenAI()\n",
        # only for type checkers
        "from typing import TYPE_CHECKING\n\nimport openai\n\nif TYPE_CHECKING:\n"
        "    from openai import OpenAI\n\n\ndef ask() -> 'OpenAI':\n    return openai.OpenAI()\n",
        # imported but never used
        "import openai\nfrom openai import OpenAI\n\n\ndef ask():\n    return openai.OpenAI()\n",
    ],
)
def test_imports_the_patch_reaches_are_not_flagged(app):
    assert app_problems({"app.py": app}) == []


def test_a_test_that_uses_the_library_name_itself_is_not_flagged():
    own = TEST_PATCH.replace("    pass\n", "    import openai as oa\n\n    oa.OpenAI()\n")
    assert app_problems({"app.py": APP_FROM}, own) == []


def test_aliases_submodules_and_files_with_no_dotted_name():
    alias = APP_FROM.replace("import OpenAI", "import OpenAI as OAI").replace("OpenAI()", "OAI()")
    # modules in the order the job lists them; another bound name is shown with the module
    assert app_problems({"app.py": alias, "other.py": alias, "more.py": APP_FROM}) == [
        'patch("openai.OpenAI") does not affect app.py, which imports OpenAI with "from openai '
        'import OpenAI as OAI"; patch "app.OAI" instead (and likewise for: other, more (as '
        "OpenAI))"
    ]
    sub = "from openai.types.chat import ChatCompletion\n\nx = ChatCompletion\n"
    test = TEST_PATCH.replace('"openai.OpenAI"', '"openai.types.chat.ChatCompletion"')
    (problem,) = app_problems({"app.py": sub}, test)
    assert problem.endswith('patch "app.ChatCompletion" instead')
    assert app_problems({"my-scripts/app.py": APP_FROM}) == []


def test_a_repo_module_named_like_the_library_is_not_this_check():
    files = {"openai.py": "OpenAI = object\n", "app.py": APP_FROM}
    assert app_problems(files) == []


# --- suggestions from the modules that use the library (indirect_reach fixture) --------------

INDIRECT = Path(__file__).parent / "fixtures" / "indirect_reach"
INDIRECT_TEST = "tests/test_context.py"
# notebook/generate.py migrated, binding the client class with a from-import
GENERATE_FROM = '''\
"""Replies from the chat model."""

from openai import OpenAI


def generate_reply(context, text):
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[*context, {"role": "user", "content": text}],
    )
    yield response.choices[0].message.content
'''
BATCH_V1 = """\
import openai


def summarize(texts):
    client = openai.OpenAI()
    response = client.chat.completions.create(model="gpt-3.5-turbo", messages=[])
    return response.choices[0].message.content
"""


def indirect_test(target: str = "notebook.context.OpenAI", form: str = "decorator") -> str:
    """tests/test_context.py migrated to objects, patching `target` (the client class)."""
    original = (INDIRECT / INDIRECT_TEST).read_text()
    if form == "mocker":
        test = (
            "def test_assist_adds_the_reply_to_the_context(mocker, shell):\n"
            f'    OpenAI = mocker.patch("{target}")\n'
        )
    else:
        test = (
            f'@patch("{target}")\ndef test_assist_adds_the_reply_to_the_context(OpenAI, shell):\n'
        )
    test += (
        "    OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(\n"
        "        choices=[\n"
        "            SimpleNamespace(\n"
        '                message=SimpleNamespace(role="assistant", content="plot(df)")\n'
        "            )\n"
        "        ]\n"
        "    )\n"
    )
    old_head = original[original.index("@patch(") : original.index("    shell.run_cell")]
    text = original.replace(old_head, test)
    text = text.replace(
        "create.call_count", "OpenAI.return_value.chat.completions.create.call_count"
    )
    return "from types import SimpleNamespace\n" + text


def indirect_files(changes: dict[str, str | None] | None = None) -> dict[str, str]:
    """The fixture's Python files with generate.py migrated (GENERATE_FROM), then `changes`
    (relpath -> text; None removes the file)."""
    files = {p.relative_to(INDIRECT).as_posix(): p.read_text() for p in INDIRECT.rglob("*.py")}
    files["notebook/generate.py"] = GENERATE_FROM
    for rel, text in (changes or {}).items():
        if text is None:
            files.pop(rel, None)
        else:
            files[rel] = text
    return files


def indirect_problems(
    files: dict[str, str], candidate: str | None = None, unmigrated: tuple[str, ...] = ()
) -> list[str]:
    original = (INDIRECT / INDIRECT_TEST).read_text()
    candidate = indirect_test() if candidate is None else candidate
    tests = tuple(sorted(r for r in files if r.startswith("tests/")))
    return mock_target_problems(
        original,
        candidate,
        reader({**files, INDIRECT_TEST: candidate}),
        library_modules=OPENAI,
        mock_responses=MIGRATION.mock_responses,
        roots=module_roots(INDIRECT_TEST),
        modules=tuple(sorted(r for r in files if r not in tests)),
        test_files=tests,
        path=INDIRECT_TEST,
        unmigrated_modules=unmigrated,
    )


# no list of context.py's names: none of them reaches openai, so the suggestion comes first
INDIRECT_HEAD = (
    "mock target 'notebook.context.OpenAI': notebook/context.py has no top-level name 'OpenAI' "
    "and does not use openai. "
)
INDIRECT_EXAMPLE = (
    "Mock responses must be objects, not dicts (1.x code uses attribute access).\n"
    "For example:\n"
    '    @patch("notebook.generate.OpenAI")\n'
    "    def test_assist_adds_the_reply_to_the_context(OpenAI):\n"
    "        OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(\n"
    '            choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", '
    'content="hello"))]\n'
    "        )"
)


def test_a_module_without_the_library_gets_the_modules_the_test_reaches():
    # genai: the test imports notebook.context only; the package's __init__.py imports the
    # magics, which import notebook.generate. batch.py also calls openai; nothing reaches it.
    assert indirect_problems(indirect_files({"notebook/batch.py": BATCH_V1})) == [
        INDIRECT_HEAD + "Patch openai where a module the test reaches uses it:\n"
        "    notebook/generate.py: generate_reply() creates client = OpenAI(): "
        'patch("notebook.generate.OpenAI") and set '
        "OpenAI.return_value.chat.completions.create.return_value.\n" + INDIRECT_EXAMPLE
    ]


def test_the_suggested_target_passes_the_check():
    files = indirect_files({"notebook/batch.py": BATCH_V1})
    assert indirect_problems(files, indirect_test("notebook.generate.OpenAI")) == []


@pytest.mark.parametrize(
    ("generate", "target", "chain"),
    [
        # import openai + openai.OpenAI(): the module's openai is the library itself
        (
            GENERATE_FROM.replace("from openai import OpenAI", "import openai").replace(
                "client = OpenAI()", "client = openai.OpenAI()"
            ),
            "openai.OpenAI",
            "OpenAI.return_value.chat.completions.create.return_value",
        ),
        # also through an alias
        (
            GENERATE_FROM.replace("from openai import OpenAI", "import openai as oa").replace(
                "client = OpenAI()", "client = oa.OpenAI()"
            ),
            "openai.OpenAI",
            "OpenAI.return_value.chat.completions.create.return_value",
        ),
        # the module-level client calls
        (
            GENERATE_FROM.replace("from openai import OpenAI", "import openai")
            .replace("    client = OpenAI()\n", "")
            .replace("client.chat", "openai.chat"),
            "openai.chat.completions.create",
            "create.return_value",
        ),
        # a module-level client = OpenAI()
        (
            GENERATE_FROM.replace(
                "from openai import OpenAI\n", "from openai import OpenAI\n\nclient = OpenAI()\n"
            ).replace("    client = OpenAI()\n", ""),
            "notebook.generate.client.chat.completions.create",
            "create.return_value",
        ),
        # a factory
        (
            GENERATE_FROM.replace(
                "\n\ndef generate_reply",
                "\n\ndef get_client():\n    return OpenAI()\n\n\ndef generate_reply",
            ).replace("client = OpenAI()", "client = get_client()"),
            "notebook.generate.get_client",
            "get_client.return_value.chat.completions.create.return_value",
        ),
    ],
)
def test_each_way_of_binding_the_library_gets_its_own_target(generate, target, chain):
    (problem,) = indirect_problems(indirect_files({"notebook/generate.py": generate}))
    assert f'patch("{target}") and set {chain}.\n' in problem
    var = chain.split(".")[0]
    assert f'    @patch("{target}")\n    def test_assist_adds_the_reply_to_the_context({var}):' in (
        problem
    )


def test_when_the_test_reaches_none_every_library_module_is_listed():
    files = indirect_files(
        {"notebook/__init__.py": '"""Nothing imported."""\n', "notebook/batch.py": BATCH_V1}
    )
    (problem,) = indirect_problems(files)
    assert problem.startswith(
        INDIRECT_HEAD + "Patch openai where a module (the test reaches none of them through its "
        "imports) uses it:\n"
        "    notebook/batch.py: summarize() creates client = openai.OpenAI(): "
        'patch("openai.OpenAI") and set OpenAI.return_value.chat.completions.create.return_value.\n'
        "    notebook/generate.py: generate_reply() creates client = OpenAI(): "
        'patch("notebook.generate.OpenAI") and set '
    )
    assert '    @patch("openai.OpenAI")\n' in problem  # the example follows the first one


def test_at_most_three_modules_are_listed_and_the_rest_named():
    extra = {f"notebook/m{n}.py": BATCH_V1 for n in range(1, 4)}
    files = indirect_files({"notebook/__init__.py": "", "notebook/batch.py": BATCH_V1, **extra})
    (problem,) = indirect_problems(files)
    listed = [line for line in problem.splitlines() if line.startswith("    notebook/")]
    assert [line.split(":")[0].strip() for line in listed] == [
        "notebook/batch.py",
        "notebook/generate.py",
        "notebook/m1.py",
    ]
    assert "\n    (also: notebook/m2.py, notebook/m3.py)\n" in problem


def test_the_patched_module_comes_first_when_it_calls_the_library():
    module = GENERATE_FROM.replace("from openai import OpenAI", "import openai").replace(
        "client = OpenAI()", "client = openai.OpenAI()"
    )
    files = indirect_files({"notebook/generate.py": module})
    (problem,) = indirect_problems(files, indirect_test("notebook.generate.OpenAI"))
    assert problem.startswith(
        "mock target 'notebook.generate.OpenAI': notebook/generate.py has no top-level name "
        "'OpenAI' (its top-level names: generate_reply, openai). Patch openai where a module the "
        "test reaches uses it:\n"
        "    notebook/generate.py: generate_reply() creates client = openai.OpenAI(): "
        'patch("openai.OpenAI") and set '
    )


def test_modules_still_on_the_old_api_are_left_out():
    original = (INDIRECT / "notebook/generate.py").read_text()
    files = indirect_files({"notebook/generate.py": original, "notebook/batch.py": BATCH_V1})
    (problem,) = indirect_problems(files, unmigrated=("notebook/generate.py",))
    assert "notebook/generate.py:" not in problem
    assert "a module (the test reaches none of them through its imports)" in problem
    assert "    notebook/batch.py: summarize() creates client = openai.OpenAI(): " in problem
    # nothing left to suggest: the generic advice
    files = indirect_files({"notebook/generate.py": original})
    (problem,) = indirect_problems(files, unmigrated=("notebook/generate.py", "notebook/batch.py"))
    assert problem.endswith("). Patch a name that exists in notebook/context.py")


def test_patching_the_code_under_test_lists_the_modules_too():
    candidate = indirect_test().replace(
        '@patch("notebook.context.OpenAI")', '@patch("notebook.context.build_context")'
    )
    (problem,) = indirect_problems(indirect_files(), candidate)
    assert problem.startswith(
        "mock target 'notebook.context.build_context' in test_assist_adds_the_reply_to_the_context:"
        " the test calls build_context() from notebook/context.py directly, so patching it "
        "replaces the function being tested; patch what it calls instead; build_context() does "
        "not call openai directly. Patch openai where a module the test reaches uses it:\n"
        "    notebook/generate.py: "
    )


def test_a_mocker_patch_gets_a_mocker_example():
    (problem,) = indirect_problems(indirect_files(), indirect_test(form="mocker"))
    assert problem.endswith(
        'For example:\n    OpenAI = mocker.patch("notebook.generate.OpenAI")\n'
        "    OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(\n"
        '        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", '
        'content="hello"))]\n'
        "    )"
    )


def _indirect_pytest(root: Path, files: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the fixture's tests with an openai stub that raises without an API key."""
    for rel, text in {**files, "openai.py": STUB_OPENAI}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_suggested_target_passes_the_tests_and_the_wrong_one_fails(tmp_path):
    wrong = _indirect_pytest(
        tmp_path / "wrong", {**indirect_files(), INDIRECT_TEST: indirect_test()}
    )
    assert wrong.returncode == 1
    assert "does not have the attribute 'OpenAI'" in wrong.stdout
    right = indirect_test("notebook.generate.OpenAI")
    fixed = _indirect_pytest(tmp_path / "right", {**indirect_files(), INDIRECT_TEST: right})
    assert fixed.returncode == 0, fixed.stdout


# --- autospec on a patched client class (autospec_client fixture) ----------------------------

AUTOSPEC = Path(__file__).parent / "fixtures" / "autospec_client"
AUTOSPEC_TEST = "tests/test_generate.py"
# assist/generate.py migrated as in genai-gptoss-4: the client class bound with a from-import
AUTOSPEC_GENERATE = '''\
"""Suggestions for a notebook cell's error."""

from openai import OpenAI


def suggest(error: str) -> str:
    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": f"Explain this error: {error}"}],
    )
    return response.choices[0].message.content
'''
REPLY = 'SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Define b"))])'
# genai-gptoss-4 attempt 1's shape: the patch moved to the client class, autospec=True kept
AUTOSPEC_KEPT = f"""\
from types import SimpleNamespace
from unittest import mock

from assist.generate import suggest


@mock.patch(
    "assist.generate.OpenAI",
    autospec=True,
)
def test_suggest(OpenAI):
    OpenAI.return_value.chat.completions.create.return_value = {REPLY}
    assert suggest("NameError: b") == "Define b"
    create = OpenAI.return_value.chat.completions.create
    create.assert_called_once()
    assert create.call_args.kwargs["model"] == "gpt-3.5-turbo"


@mock.patch("assist.generate.OpenAI", autospec=True)
def test_suggest_passes_the_error(OpenAI):
    client = OpenAI.return_value
    client.chat.completions.create.return_value = {REPLY}
    suggest("ZeroDivisionError")
    (message,) = client.chat.completions.create.call_args.kwargs["messages"]
    assert "ZeroDivisionError" in message["content"]
"""
AUTOSPEC_PROBLEM = (
    "mock target 'assist.generate.OpenAI' is patched with autospec=True, and test_suggest uses "
    "OpenAI.return_value.chat.completions (line 12) (and likewise in: "
    "test_suggest_passes_the_error): an autospecced 1.x client has no chat.completions "
    "(resources are cached properties), so drop autospec=True when patching the client class "
    '(patch("assist.generate.OpenAI")); patching the method instead leaves the real client '
    "being built, so the test would also need a placeholder API key"
)


def autospec_problems(
    candidate: str,
    original: str | None = None,
    generate: str = AUTOSPEC_GENERATE,
    **kwargs,
) -> list[str]:
    original = (AUTOSPEC / AUTOSPEC_TEST).read_text() if original is None else original
    files = {"assist/__init__.py": "", "assist/generate.py": generate, AUTOSPEC_TEST: candidate}
    return mock_target_problems(
        original,
        candidate,
        reader(files),
        library_modules=OPENAI,
        mock_responses=MIGRATION.mock_responses,
        roots=module_roots(AUTOSPEC_TEST),
        modules=("assist/__init__.py", "assist/generate.py"),
        test_files=(AUTOSPEC_TEST,),
        path=AUTOSPEC_TEST,
        **{
            "client_classes": MIGRATION.client_classes,
            "client_resources": MIGRATION.client_resources,
            **kwargs,
        },
    )


def test_autospec_kept_on_the_client_class_is_rejected():
    assert autospec_problems(AUTOSPEC_KEPT) == [AUTOSPEC_PROBLEM]
    # the regions' error attribution finds the target in the message
    assert AUTOSPEC_PROBLEM.startswith("mock target 'assist.generate.OpenAI'")


def test_autospec_needs_the_migrations_client_tables():
    assert autospec_problems(AUTOSPEC_KEPT, client_classes=(), client_resources=()) == []


def _one_test(decorators: str, params: str, body: str) -> str:
    head = "from types import SimpleNamespace\nfrom unittest import mock\n\n"
    head += "from assist.generate import suggest\n\n\n"
    return f"{head}{decorators}def test_suggest({params}):\n{body}"


SET_REPLY = f"    OpenAI.return_value.chat.completions.create.return_value = {REPLY}\n"


@pytest.mark.parametrize(
    ("test", "used"),
    [
        # decorator below another patch: the mock is the first parameter
        (
            _one_test(
                '@mock.patch("builtins.print")\n'
                '@mock.patch("assist.generate.OpenAI", autospec=True)\n',
                "OpenAI, print_",
                SET_REPLY,
            ),
            "OpenAI.return_value.chat.completions",
        ),
        # decorator above another patch: the second parameter
        (
            _one_test(
                '@mock.patch("assist.generate.OpenAI", autospec=True)\n'
                '@mock.patch("builtins.print")\n',
                "print_, OpenAI",
                SET_REPLY,
            ),
            "OpenAI.return_value.chat.completions",
        ),
        # context manager
        (
            _one_test(
                "",
                "",
                '    with mock.patch("assist.generate.OpenAI", autospec=True) as OpenAI:\n'
                f"    {SET_REPLY}        suggest('x')\n",
            ),
            "OpenAI.return_value.chat.completions",
        ),
        # mocker.patch, and the instance through a name
        (
            _one_test(
                "",
                "mocker",
                '    OpenAI = mocker.patch("assist.generate.OpenAI", autospec=True)\n'
                "    client = OpenAI.return_value\n"
                "    client.embeddings.create.return_value = None\n",
            ),
            "client.embeddings.create",
        ),
        # patch(...).start()
        (
            _one_test(
                "",
                "",
                '    OpenAI = mock.patch("assist.generate.OpenAI", autospec=True).start()\n'
                + SET_REPLY,
            ),
            "OpenAI.return_value.chat.completions",
        ),
        # the library's own path, the async client, a resource used on its own
        (
            _one_test(
                '@mock.patch("openai.AsyncOpenAI", autospec=True)\n',
                "OpenAI",
                "    run(OpenAI.return_value.chat)\n",
            ),
            "OpenAI.return_value.chat",
        ),
    ],
)
def test_autospec_forms_that_give_the_mock_a_name(test, used):
    (problem,) = [p for p in autospec_problems(test) if "autospec" in p]
    assert f"test_suggest uses {used} (line " in problem


def test_autospec_on_a_class_decorator_reaches_each_test_method():
    test = (
        "from unittest import mock\n\n\n"
        '@mock.patch("assist.generate.OpenAI", autospec=True)\n'
        "class TestSuggest:\n"
        '    @mock.patch("builtins.print")\n'
        "    def test_a(self, print_, OpenAI):\n"
        "        OpenAI.return_value.chat.completions.create.return_value = None\n\n"
        "    def test_b(self, OpenAI):\n"
        "        OpenAI.return_value.chat.completions.create.return_value = None\n\n"
        "    def helper(self, OpenAI):\n"
        "        OpenAI.return_value.chat.completions.create.return_value = None\n"
    )
    (problem,) = autospec_problems(test)
    assert (
        "test_a uses OpenAI.return_value.chat.completions (line 8) (and likewise in: test_b)"
        in (problem)
    )


def test_a_client_class_bound_under_another_name_is_recognised():
    generate = AUTOSPEC_GENERATE.replace("import OpenAI", "import OpenAI as Client").replace(
        "OpenAI()", "Client()"
    )
    test = AUTOSPEC_KEPT.replace("assist.generate.OpenAI", "assist.generate.Client")
    (problem,) = autospec_problems(test, generate=generate)
    assert problem.startswith("mock target 'assist.generate.Client' is patched with autospec=True")


@pytest.mark.parametrize(
    "test",
    [
        # no autospec, or not True
        AUTOSPEC_KEPT.replace(",\n    autospec=True,\n", ",\n").replace(", autospec=True", ""),
        AUTOSPEC_KEPT.replace("autospec=True", "autospec=False"),
        # the test gives the instance itself
        _one_test(
            '@mock.patch("assist.generate.OpenAI", autospec=True)\n',
            "OpenAI",
            "    OpenAI.return_value = mock.Mock()\n" + SET_REPLY,
        ),
        _one_test(
            '@mock.patch("assist.generate.OpenAI", autospec=True, return_value=mock.Mock())\n',
            "OpenAI",
            SET_REPLY,
        ),
        # no resource used: autospec only checks the constructor's arguments
        _one_test(
            '@mock.patch("assist.generate.OpenAI", autospec=True)\n',
            "OpenAI",
            "    suggest('x')\n    OpenAI.assert_called_once_with()\n",
        ),
        # not a client class
        _one_test(
            '@mock.patch("builtins.print", autospec=True)\n',
            "print_",
            "    print_.return_value.chat.completions\n",
        ),
        # new= given: the decorator passes no mock, so OpenAI is another parameter
        _one_test(
            '@mock.patch("assist.generate.OpenAI", mock.Mock(), autospec=True)\n',
            "OpenAI",
            SET_REPLY,
        ),
    ],
)
def test_autospec_cases_that_are_not_flagged(test):
    assert [p for p in autospec_problems(test) if "autospec" in p] == []


def test_autospec_already_in_the_original_is_not_flagged():
    assert autospec_problems(AUTOSPEC_KEPT, original=AUTOSPEC_KEPT) == []
    # a new test with the same problem is
    more = AUTOSPEC_KEPT.replace("def test_suggest_passes_the_error", "def test_other")
    (problem,) = autospec_problems(more, original=AUTOSPEC_KEPT)
    assert "and test_other uses client.chat.completions (line 22)" in problem


def test_autospec_is_a_mock_targets_error_in_whole_file_checks():
    from apifix.engine.whole_file import check_candidate

    original = (AUTOSPEC / AUTOSPEC_TEST).read_text()
    files = {"assist/__init__.py": "", "assist/generate.py": AUTOSPEC_GENERATE}
    result = check_candidate(
        AUTOSPEC_TEST,
        original,
        AUTOSPEC_KEPT,
        required=True,
        read_module=reader(files),
        library_modules=OPENAI,
        module_roots=module_roots(AUTOSPEC_TEST),
        python_modules=tuple(files),
        test_files=(AUTOSPEC_TEST,),
        client_classes=MIGRATION.client_classes,
        client_resources=MIGRATION.client_resources,
    )
    assert result.errors == [AUTOSPEC_PROBLEM]
    assert result.error_checks == ["mock_targets"]


# An openai stub whose client, like 1.x's, has its resources as cached properties.
STUB_CLIENT = """\
from functools import cached_property


class Completions:
    def create(self, **kwargs):
        raise RuntimeError("no network in tests")


class Chat:
    @cached_property
    def completions(self):
        return Completions()


class OpenAI:
    def __init__(self, **kwargs):
        pass

    @cached_property
    def chat(self):
        return Chat()
"""


def _autospec_pytest(root: Path, test: str) -> subprocess.CompletedProcess:
    files = {
        "openai.py": STUB_CLIENT,
        "assist/__init__.py": "",
        "assist/generate.py": AUTOSPEC_GENERATE,
        AUTOSPEC_TEST: test,
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
    )


# The other fix the message offers: autospec the method (the stub's Completions.create).
AUTOSPEC_METHOD = f"""\
from types import SimpleNamespace
from unittest import mock

from assist.generate import suggest


@mock.patch("openai.Completions.create", autospec=True)
def test_suggest(create):
    create.return_value = {REPLY}
    assert suggest("NameError: b") == "Define b"
    create.assert_called_once()
    assert create.call_args.kwargs["model"] == "gpt-3.5-turbo"
"""


def test_autospec_on_the_client_fails_and_the_suggested_fixes_pass(tmp_path):
    kept = _autospec_pytest(tmp_path / "kept", AUTOSPEC_KEPT)
    assert kept.returncode == 1
    assert "Mock object has no attribute 'completions'" in kept.stdout
    dropped = AUTOSPEC_KEPT.replace(",\n    autospec=True,\n", ",\n").replace(", autospec=True", "")
    assert autospec_problems(dropped) == []
    assert _autospec_pytest(tmp_path / "dropped", dropped).returncode == 0
    assert autospec_problems(AUTOSPEC_METHOD) == []
    result = _autospec_pytest(tmp_path / "method", AUTOSPEC_METHOD)
    assert result.returncode == 0, result.stdout
