from __future__ import annotations

import textwrap

import pytest

from apifix.engine.tampering import check_test_config, check_test_file


def edits(old: str, new: str | None, file: str = "tests/test_chat.py"):
    return check_test_file(
        file,
        textwrap.dedent(old),
        textwrap.dedent(new) if new is not None else None,
        api_modules=frozenset({"openai"}),
        api_broad_exceptions=frozenset({"OpenAIError", "APIError", "APIStatusError"}),
    )


def kinds(old: str, new: str | None) -> list[tuple[str, str, str | None]]:
    return [(e.kind, e.severity, e.test) for e in edits(old, new)]


BASE = """
    from unittest import mock

    import openai
    import pytest

    from app import answer


    @mock.patch("openai.ChatCompletion.create")
    def test_answer(create):
        create.return_value = {"choices": [{"message": {"content": "hi"}}]}
        assert answer("q") == "hi"
        create.assert_called_once_with(model="gpt-4", messages=mock.ANY)


    def test_rate_limit():
        with pytest.raises(openai.error.RateLimitError):
            raise openai.error.RateLimitError("x")
"""


# --- allowed edits --------------------------------------------------------------------------


def test_migrating_patch_targets_and_imports_is_silent():
    new = BASE.replace('"openai.ChatCompletion.create"', '"app.client.chat.completions.create"')
    new = new.replace("openai.error.RateLimitError", "openai.RateLimitError")
    new = new.replace("    import openai\n", "    import openai\n    from openai import OpenAI\n")
    assert edits(BASE, new) == []


def test_api_renames_are_only_silent_for_the_migrated_module():
    old = "def test_x():\n    with pytest.raises(openai.error.Timeout):\n        f()\n"
    new = old.replace("openai.error.Timeout", "openai.APITimeoutError")
    assert edits(old, new) == []
    plain = check_test_file("tests/test_x.py", old, new)  # no api_modules given
    assert [e.kind for e in plain] == ["setup_changed"]


def test_api_renames_through_dotted_modules_and_their_aliases_are_silent():
    google = frozenset({"google.generativeai", "google.genai"})
    old = (
        "import google.generativeai as genai\n"
        "def test_x():\n"
        "    with pytest.raises(genai.types.BlockedPromptException):\n"
        "        f()\n"
        "    assert google.generativeai.types.HarmCategory.X == 1\n"
    )
    new = (
        "from google import genai\n"
        "def test_x():\n"
        "    with pytest.raises(genai.errors.ClientError):\n"
        "        f()\n"
        "    assert google.genai.types.HarmCategory.X == 1\n"
    )
    assert check_test_file("tests/test_x.py", old, new, api_modules=google) == []
    # a sibling package under the same top-level name is not the migrated library
    sibling = new.replace("google.genai.types.HarmCategory", "google.protobuf.HarmCategory")
    found = check_test_file("tests/test_x.py", old, sibling, api_modules=google)
    assert [e.kind for e in found] == ["setup_changed"]


def test_response_access_rewrite_in_asserts_is_silent():
    old = """
        def test_x(resp):
            assert resp["choices"][0]["message"]["content"] == "hi"
    """
    new = """
        def test_x(resp):
            assert resp.choices[0].message.content == "hi"
    """
    assert edits(old, new) == []


def test_mock_data_change_is_info_with_line():
    new = BASE.replace(
        'create.return_value = {"choices": [{"message": {"content": "hi"}}]}',
        'create.return_value = make_completion("hi")',
    )
    result = edits(BASE, new)
    assert [(e.kind, e.severity, e.test, e.line) for e in result] == [
        ("setup_changed", "info", "test_answer", 12)
    ]


def test_fixture_change_is_info():
    old = """
        import pytest

        @pytest.fixture
        def fake_response():
            return {"choices": []}

        def test_x(fake_response):
            assert fake_response is not None
    """
    new = old.replace('return {"choices": []}', "return Completion(choices=[])")
    (edit,) = edits(old, new)
    assert (edit.kind, edit.severity, edit.detail, edit.line) == (
        "setup_changed",
        "info",
        "fake_response changed",
        6,
    )


UNITTEST_BASE = """
    import unittest
    from unittest import mock


    class TestChat(unittest.TestCase):
        REPLY = {"choices": [{"message": {"content": "hi"}}]}

        def setUp(self):
            self.patcher = mock.patch("openai.ChatCompletion.create")
            self.create = self.patcher.start()
            self.create.return_value = self.REPLY

        def test_answer(self):
            self.assertEqual(answer("q"), "hi")
"""


def test_unittest_setup_change_is_info():
    new = UNITTEST_BASE.replace(
        "self.create.return_value = self.REPLY", "self.create.return_value = make(self.REPLY)"
    )
    (edit,) = edits(UNITTEST_BASE, new)
    assert (edit.kind, edit.severity, edit.detail, edit.line) == (
        "setup_changed",
        "info",
        "TestChat.setUp changed",
        12,
    )


def test_unittest_class_data_change_is_info():
    new = UNITTEST_BASE.replace(
        'REPLY = {"choices": [{"message": {"content": "hi"}}]}', 'REPLY = completion("hi")'
    )
    (edit,) = edits(UNITTEST_BASE, new)
    assert (edit.kind, edit.detail, edit.line) == ("setup_changed", "TestChat.REPLY changed", 7)


def test_unittest_patch_target_migration_in_setup_is_silent():
    new = UNITTEST_BASE.replace(
        '"openai.ChatCompletion.create"', '"app.client.chat.completions.create"'
    )
    assert edits(UNITTEST_BASE, new) == []


def test_renamed_test_is_info():
    old = "def test_old():\n    assert 1 == 1\n"
    new = "def test_new():\n    assert 1 == 1\n"
    assert kinds(old, new) == [("test_renamed", "info", "test_old")]


def test_new_test_file_has_nothing_to_compare():
    assert edits("", "def test_x():\n    assert True\n") == []
    assert check_test_file("tests/test_new.py", None, "def test_x():\n    assert 1\n") == []


def test_extra_asserts_are_fine():
    old = "def test_x():\n    assert f() == 1\n"
    new = "def test_x():\n    assert f() == 1\n    assert g() == 2\n"
    assert kinds(old, new) == [("setup_changed", "info", "test_x")]


# --- rejected edits -------------------------------------------------------------------------


def test_removed_test():
    new = BASE.split("def test_rate_limit")[0]
    assert ("test_removed", "reject", "test_rate_limit") in kinds(BASE, new)


def test_deleted_test_file_removes_every_test():
    assert sorted(kinds(BASE, None)) == [
        ("test_removed", "reject", "test_answer"),
        ("test_removed", "reject", "test_rate_limit"),
    ]


def test_removed_assert():
    new = BASE.replace(
        '        create.assert_called_once_with(model="gpt-4", messages=mock.ANY)\n', ""
    )
    result = edits(BASE, new)
    assert [(e.kind, e.detail) for e in result] == [("assert_removed", "2 -> 1 assertions")]


@pytest.mark.parametrize(
    ("old_assert", "new_assert"),
    [
        ('assert answer("q") == "hi"', 'assert answer("q")'),
        ('assert answer("q") == "hi"', 'assert answer("q") is not None'),
        ('assert answer("q") == "hi"', "assert True"),
        ('assert answer("q") == "hi"', 'assert "hi" in answer("q")'),
        ("self.assertEqual(answer('q'), 'hi')", "self.assertTrue(answer('q'))"),
        ("self.assertEqual(answer('q'), 'hi')", "self.assertTrue(True)"),
    ],
)
def test_weakened_assert(old_assert, new_assert):
    old = f"def test_x(self):\n    {old_assert}\n"
    new = f"def test_x(self):\n    {new_assert}\n"
    assert kinds(old, new) == [("assert_weakened", "reject", "test_x")]


def test_changed_expected_value_is_a_warning():
    old = 'def test_x():\n    assert answer("q") == "hi"\n'
    new = 'def test_x():\n    assert answer("q") == "hello"\n'
    assert kinds(old, new) == [("expected_value_changed", "warn", "test_x")]


@pytest.mark.parametrize(
    "decorator",
    [
        "@pytest.mark.skip(reason='later')",
        "@pytest.mark.xfail",
        "@pytest.mark.skipif(True, reason='x')",
        "@unittest.skip('x')",
        "@unittest.expectedFailure",
    ],
)
def test_added_skip_decorator(decorator):
    old = "def test_x():\n    assert f() == 1\n"
    new = f"{decorator}\ndef test_x():\n    assert f() == 1\n"
    assert kinds(old, new) == [("skip_added", "reject", "test_x")]


@pytest.mark.parametrize(
    "call",
    ["pytest.skip('x')", "pytest.xfail('x')", "self.skipTest('x')", "raise unittest.SkipTest('x')"],
)
def test_added_skip_call(call):
    old = "def test_x(self):\n    assert f() == 1\n"
    new = f"def test_x(self):\n    {call}\n    assert f() == 1\n"
    assert kinds(old, new) == [("skip_added", "reject", "test_x")]


def test_skip_on_test_class():
    old = "class TestChat:\n    def test_a(self):\n        assert f() == 1\n"
    new = "@pytest.mark.skip\nclass TestChat:\n    def test_a(self):\n        assert f() == 1\n"
    assert kinds(old, new) == [("skip_added", "reject", "TestChat.test_a")]


def test_module_pytestmark_skip_added():
    old = "def test_a():\n    assert f() == 1\n"
    new = "import pytest\npytestmark = pytest.mark.skip\n\ndef test_a():\n    assert f() == 1\n"
    assert kinds(old, new) == [("skip_added", "reject", None)]


def test_changed_skip_condition_is_flagged_for_runtime_check():
    old = "@pytest.mark.skipif(sys.version_info < (3, 8))\ndef test_x():\n    assert 1\n"
    new = old.replace("(3, 8)", "(9, 9)")
    assert kinds(old, new) == [("skip_condition_changed", "warn", "test_x")]


@pytest.mark.parametrize(
    "wrapped",
    [
        "    try:\n        assert f() == 1\n    except Exception:\n        pass\n",
        "    try:\n        assert f() == 1\n    except AssertionError:\n        pass\n",
        "    try:\n        assert f() == 1\n    except:\n        pass\n",
    ],
)
def test_broad_except_added(wrapped):
    old = "def test_x():\n    assert f() == 1\n"
    new = "def test_x():\n" + wrapped
    assert ("except_broadened", "reject", "test_x") in kinds(old, new)


def test_raises_broadened():
    new = BASE.replace(
        "with pytest.raises(openai.error.RateLimitError):", "with pytest.raises(Exception):"
    )
    assert ("except_broadened", "reject", "test_rate_limit") in kinds(BASE, new)


def test_raises_broadened_to_api_base_class():
    new = BASE.replace(
        "with pytest.raises(openai.error.RateLimitError):", "with pytest.raises(openai.APIError):"
    )
    assert ("except_broadened", "reject", "test_rate_limit") in kinds(BASE, new)


def test_collection_hook_added_in_conftest():
    old = "import pytest\n"
    new = "import pytest\n\ndef pytest_collection_modifyitems(items):\n    items.clear()\n"
    result = check_test_file("conftest.py", old, new)
    assert [(e.kind, e.severity, e.line) for e in result] == [("test_config_changed", "reject", 3)]


@pytest.mark.parametrize("old", [None, "", "X = 1\n"])
def test_collect_ignore_added(old):
    new = (old or "") + "collect_ignore = ['tests/test_chat.py']\n"
    result = check_test_file("conftest.py", old, new)
    assert [(e.kind, e.severity) for e in result] == [("test_config_changed", "reject")]


def test_unparseable_test_file():
    result = edits(BASE, "def test_x(:\n")
    assert [(e.kind, e.severity) for e in result] == [("test_file_unparseable", "reject")]


# --- pytest config files --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "old", "new"),
    [
        (
            "pytest.ini",
            "[pytest]\naddopts = -q\n",
            "[pytest]\naddopts = -q --deselect tests/x.py\n",
        ),
        ("tox.ini", "[pytest]\ntestpaths = tests\n", "[pytest]\ntestpaths = tests/unit\n"),
        (
            "setup.cfg",
            "[metadata]\nname = x\n",
            "[metadata]\nname = x\n[tool:pytest]\naddopts = -k chat\n",
        ),
        (
            "pyproject.toml",
            "[tool.pytest.ini_options]\naddopts = '-q'\n",
            "[tool.pytest.ini_options]\naddopts = '-q --ignore=tests/test_chat.py'\n",
        ),
    ],
)
def test_pytest_config_changes_are_rejected(file, old, new):
    (edit,) = check_test_config(file, old, new)
    assert (edit.kind, edit.severity) == ("test_config_changed", "reject")


def test_unrelated_config_changes_are_not_test_config():
    old = (
        "[project]\ndependencies = ['openai==0.28.1']\n[tool.pytest.ini_options]\naddopts = '-q'\n"
    )
    new = old.replace("openai==0.28.1", "openai>=1.0")
    assert check_test_config("pyproject.toml", old, new) == []
    assert check_test_config("README.md", "a", "b") == []
