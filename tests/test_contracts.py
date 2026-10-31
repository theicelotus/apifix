"""Test files that patch code a migration rewrites (contracts.contract_tests), the prompt section
they get, and whole-file mode end to end."""

from __future__ import annotations

from pathlib import Path

import pytest

from apifix.engine.contracts import contract_tests
from apifix.engine.whole_file import file_prompt, send_order
from tests.test_agent import APP_NEW, FakeLLM, JudgingSandbox, block, files_of, first_lines, wf
from tests.test_verifier import ORIGINAL, write_tree

FIXTURE = Path(__file__).parent / "fixtures" / "google_genai_repos" / "ailabel_like"


def tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


MOCK = "from unittest import mock\n\n"
LLMS = (
    "def get_gemini():\n    return object()\n\n\nclass Gemini:\n    def ask(self):\n        pass\n"
)


def test_the_ailabel_shape_is_found_directly_and_where_used():
    out = contract_tests(FIXTURE, ["ailabel/lib/llms.py"], skip=["tests/test_llms.py"])
    assert sorted(out) == ["tests/test_direct.py", "tests/test_label.py"]
    where_used = out["tests/test_label.py"][0]
    assert (where_used.target, where_used.name) == ("ailabel.lib.consumer.get_gemini", "get_gemini")
    assert where_used.defined_in == "ailabel/lib/llms.py"
    assert out["tests/test_direct.py"][0].target == "ailabel.lib.llms.get_gemini"


def test_files_already_sent_and_unrelated_patches_are_left_out():
    out = contract_tests(FIXTURE, ["ailabel/lib/llms.py"])
    assert "tests/test_other.py" not in out  # patches ailabel.lib.other.thing
    # test_llms.py patches names llms imports (genai), which llms does not define
    assert "tests/test_llms.py" not in out


@pytest.mark.parametrize(
    ("test", "name"),
    [
        (
            'from unittest.mock import patch\n\n@patch("pkg.llms.get_gemini")\ndef t(m): ...\n',
            "get_gemini",
        ),
        (
            'def t(monkeypatch):\n    monkeypatch.setattr("pkg.llms.get_gemini", None)\n',
            "get_gemini",
        ),
        (
            MOCK + 'from pkg import llms\n\nmock.patch.object(llms, "get_gemini")\n',
            "get_gemini",
        ),
        ('from unittest import mock\n\nmock.patch("pkg.llms.Gemini.ask")\n', "Gemini"),
    ],
)
def test_patch_forms(tmp_path, test, name):
    repo = tree(tmp_path, {"pkg/__init__.py": "", "pkg/llms.py": LLMS, "tests/test_x.py": test})
    out = contract_tests(repo, ["pkg/llms.py"])
    assert [(p.name, p.defined_in) for p in out["tests/test_x.py"]] == [(name, "pkg/llms.py")]


def test_a_src_layout_matches_by_dotted_suffix(tmp_path):
    repo = tree(
        tmp_path,
        {
            "src/pkg/llms.py": LLMS,
            "tests/test_x.py": 'from unittest import mock\n\nmock.patch("pkg.llms.get_gemini")\n',
        },
    )
    assert list(contract_tests(repo, ["src/pkg/llms.py"])) == ["tests/test_x.py"]


def test_where_used_through_a_relative_import(tmp_path):
    repo = tree(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/llms.py": LLMS,
            "pkg/use.py": "from .llms import get_gemini\n",
            "tests/test_x.py": MOCK + 'mock.patch("pkg.use.get_gemini")\n',
        },
    )
    assert list(contract_tests(repo, ["pkg/llms.py"])) == ["tests/test_x.py"]


def test_a_name_the_rewritten_file_only_imports_is_not_a_contract(tmp_path):
    repo = tree(
        tmp_path,
        {
            "pkg/llms.py": "import os\n\ndef f(): ...\n",
            "tests/test_x.py": MOCK + 'mock.patch("pkg.llms.os")\n',
        },
    )
    assert contract_tests(repo, ["pkg/llms.py"]) == {}


def test_only_non_test_python_files_count_as_rewritten(tmp_path):
    repo = tree(
        tmp_path,
        {
            "pkg/llms.py": LLMS,
            "tests/test_helpers.py": "def helper(): ...\n",
            "tests/test_x.py": MOCK + 'mock.patch("tests.test_helpers.helper")\n',
            "requirements.txt": "x\n",
        },
    )
    assert contract_tests(repo, ["tests/test_helpers.py", "requirements.txt"]) == {}


def test_a_broken_file_is_ignored(tmp_path):
    repo = tree(tmp_path, {"pkg/llms.py": LLMS, "tests/test_x.py": "def (:\n"})
    assert contract_tests(repo, ["pkg/llms.py"]) == {}


def test_contract_section_in_the_prompt():
    out = contract_tests(FIXTURE, ["ailabel/lib/llms.py"])
    patches = out["tests/test_label.py"]
    text = (FIXTURE / "tests/test_label.py").read_text()
    prompt = file_prompt(
        "tests/test_label.py", text, [], summary="s", required=False, contract=patches
    )
    assert "Scanner findings in this file" not in prompt
    assert (
        "This test has no scanner findings, but it patches code in a file this migration rewrites"
        in prompt
    )
    assert (
        "- 6: patches `ailabel.lib.consumer.get_gemini`: `get_gemini` is defined in "
        "ailabel/lib/llms.py" in prompt
    )
    assert "Keep every assertion" in prompt


def test_send_order_puts_tests_last(tmp_path):
    repo = tree(tmp_path, {"a.py": "", "tests/test_a.py": "", "requirements.txt": ""})
    assert send_order(["tests/test_a.py", "a.py", "requirements.txt"], repo) == [
        "requirements.txt",
        "a.py",
        "tests/test_a.py",
    ]


# --- whole-file mode, end to end ------------------------------------------------------------

HELPERS_OLD = """\
from unittest import mock

import app


def fake_answer():
    return {"text": "hi"}


def test_wrapper():
    with mock.patch("app.ask") as ask:
        ask.return_value = fake_answer()
        assert app.ask("q") is ask.return_value
"""
HELPERS_NEW = HELPERS_OLD.replace('return {"text": "hi"}', 'return "hi"')


@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path / "repo", {**ORIGINAL, "tests/test_helpers.py": HELPERS_OLD})


def test_a_test_that_patches_a_rewritten_function_is_sent_last_and_may_change_anywhere(repo):
    llm = FakeLLM([block(APP_NEW), block(_tests_new()), block(HELPERS_NEW)])
    result = wf(repo, llm, JudgingSandbox())
    assert result.outcome == "verified"
    assert first_lines(llm) == [
        "Migrate this file: app.py",
        "Migrate this file: tests/test_app.py",
        "Migrate this file: tests/test_helpers.py",
    ]
    prompt = llm.requests[2][0].text
    assert "patches `app.ask`: `ask` is defined in app.py" in prompt
    assert "Return the complete updated tests/test_helpers.py" in prompt
    assert "If the file needs no change, return it unchanged." in prompt
    # app.py, rewritten, is shown as context
    assert "Files already updated for this migration" in prompt and "--- app.py" in prompt
    entry = files_of(result)[2]
    assert entry["path"] == "tests/test_helpers.py" and entry["change_required"] is False
    assert entry["contract_patches"] == [
        "11: patches `app.ask`: `ask` is defined in app.py, which this migration rewrites"
    ]
    # the edit is far from the patch line and there is no finding: still kept, not reverted
    assert entry["accepted"] and entry["reverted"] == []
    assert list(result.trajectory["contract_tests"]) == ["tests/test_helpers.py"]


def test_the_contract_test_may_come_back_unchanged(repo):
    llm = FakeLLM([block(APP_NEW), block(_tests_new()), block(HELPERS_OLD)])
    result = wf(repo, llm, JudgingSandbox())
    assert result.outcome == "verified" and files_of(result)[2]["accepted"]


def test_no_extra_file_without_a_patch_of_rewritten_code(tmp_path):
    other = HELPERS_OLD.replace('"app.ask"', '"os.getcwd"')
    repo = write_tree(tmp_path / "repo", {**ORIGINAL, "tests/test_helpers.py": other})
    llm = FakeLLM([block(APP_NEW), block(_tests_new())])
    result = wf(repo, llm, JudgingSandbox())
    assert first_lines(llm) == ["Migrate this file: app.py", "Migrate this file: tests/test_app.py"]
    assert result.trajectory["contract_tests"] == {}


def _tests_new() -> str:
    from tests.test_verifier import TESTS_NEW

    return TESTS_NEW
