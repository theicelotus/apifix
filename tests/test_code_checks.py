"""Static checks on a rewritten file: undefined names (pyflakes) and mock targets."""

from __future__ import annotations

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
