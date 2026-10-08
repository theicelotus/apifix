from __future__ import annotations

import pytest

from apifix.engine.agent_tools import (
    MAX_LIST_ENTRIES,
    MAX_READ_LINES,
    TOOLS,
    Workspace,
    editable,
)
from apifix.engine.llm import ToolCall, validate_tool_input


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / "app.py").write_text("import openai\n\nX = 1\nY = 1\n")
    (root / "pkg" / "util.py").write_text("def f():\n    return 1\n")
    (root / "tests" / "test_app.py").write_text("def test_x():\n    assert True\n")
    (root / "requirements.txt").write_text("openai==0.28.1\n")
    (root / "README.md").write_text("# docs\n")
    (root / ".git" / "config").write_text("[remote]\nurl = secret\n")
    (root / "data.bin").write_bytes(b"\xff\xfe\x00")
    return Workspace(root)


def run(ws, name, **args):
    return ws.run(ToolCall("t1", name, args))


def test_tool_specs_are_strict_json_schemas():
    for t in TOOLS:
        assert t.input_schema["type"] == "object"
        assert t.input_schema["additionalProperties"] is False


def test_read_file_numbers_lines(ws):
    out = run(ws, "read_file", path="app.py")
    assert not out.is_error
    assert out.content.splitlines()[0] == "app.py (lines 1-4 of 4)"
    assert "     3\tX = 1" in out.content


def test_read_file_pages_long_files(ws):
    (ws.root / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(5000)))
    out = run(ws, "read_file", path="big.py")
    assert (
        f"(lines 1-{MAX_READ_LINES} of 5000); more: read_file with start_line=2001" in out.content
    )
    out = run(ws, "read_file", path="big.py", start_line=4999)
    assert out.content.splitlines()[0] == "big.py (lines 4999-5000 of 5000)"


def test_read_file_errors(ws):
    assert "no such file" in run(ws, "read_file", path="nope.py").content
    assert "not a UTF-8 text file" in run(ws, "read_file", path="data.bin").content
    assert "not a file" in run(ws, "read_file", path="pkg").content


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/etc/passwd", "relative to the repo root"),
        ("../outside.py", "without '..'"),
        ("pkg/../../x", "without '..'"),
        (".git/config", "tooling directories"),
        ("", "path is required"),
    ],
)
def test_paths_outside_the_repo_are_refused(ws, path, message):
    out = run(ws, "read_file", path=path)
    assert out.is_error and message in out.content


def test_symlinks_are_not_followed(ws, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    (ws.root / "link.py").symlink_to(secret)
    (ws.root / "linkdir").symlink_to(tmp_path)
    assert "symlinks are not followed" in run(ws, "read_file", path="link.py").content
    assert "symlinks are not followed" in run(ws, "read_file", path="linkdir/secret.txt").content
    out = run(ws, "edit_file", path="link.py", old_string="token", new_string="x")
    assert out.is_error and secret.read_text() == "token"


def test_list_files_skips_tooling_dirs(ws):
    out = run(ws, "list_files").content.splitlines()
    assert "app.py" in out and "tests/test_app.py" in out
    assert not any(line.startswith(".git") for line in out)
    assert run(ws, "list_files", glob="*.py").content.splitlines() == [
        "app.py",
        "pkg/util.py",
        "tests/test_app.py",
    ]
    assert run(ws, "list_files", directory="tests", glob="tests/**/*.py").content == (
        "tests/test_app.py"
    )


def test_list_files_is_bounded(ws):
    for i in range(MAX_LIST_ENTRIES + 5):
        (ws.root / "pkg" / f"m{i:04}.py").write_text("")
    out = run(ws, "list_files", directory="pkg").content
    assert out.endswith("... and 6 more (narrow with glob)")


def test_search(ws):
    assert run(ws, "search", pattern=r"^import openai").content == "app.py:1: import openai"
    assert run(ws, "search", pattern="secret").content == "(no matches)"  # .git is invisible
    assert run(ws, "search", pattern="X|Y", glob="app.py").content.count("\n") == 1
    out = run(ws, "search", pattern="(")
    assert out.is_error and "invalid regular expression" in out.content


def test_edit_file(ws):
    out = run(ws, "edit_file", path="app.py", old_string="X = 1", new_string="X = 2")
    assert not out.is_error and out.content == "app.py: replaced 1 occurrence(s)"
    assert (ws.root / "app.py").read_text() == "import openai\n\nX = 2\nY = 1\n"
    assert out.edit.accepted and ws.edits == [out.edit]


def test_edit_requires_a_unique_match_unless_replace_all(ws):
    out = run(ws, "edit_file", path="app.py", old_string="= 1", new_string="= 3")
    assert out.is_error and "appears 2 times" in out.content
    out = run(ws, "edit_file", path="app.py", old_string="= 1", new_string="= 3", replace_all=True)
    assert out.edit.replacements == 2
    assert (ws.root / "app.py").read_text().count("= 3") == 2


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"path": "app.py", "old_string": "nope", "new_string": "x"}, "old_string not found"),
        ({"path": "app.py", "old_string": "", "new_string": "x"}, "old_string is empty"),
        ({"path": "app.py", "old_string": "X", "new_string": "X"}, "identical"),
        ({"path": "app.py", "old_string": "X = 1", "new_string": "X = ("}, "break the syntax"),
        ({"path": "README.md", "old_string": "docs", "new_string": "x"}, "may be edited"),
        ({"path": "new.py", "old_string": "a", "new_string": "b"}, "no such file"),
        ({"path": "pkg", "old_string": "a", "new_string": "b"}, "files cannot be created"),
    ],
)
def test_refused_edits_are_recorded(ws, args, message):
    before = {p: p.read_bytes() for p in ws.root.rglob("*") if p.is_file()}
    out = run(ws, "edit_file", **args)
    assert out.is_error and message in out.content
    assert out.edit is not None and not out.edit.accepted and message in out.edit.error
    assert {p: p.read_bytes() for p in ws.root.rglob("*") if p.is_file()} == before


def test_python_without_findings_dependency_and_test_files_are_editable(ws):
    for path, old in [
        ("pkg/util.py", "return 1"),
        ("tests/test_app.py", "assert True"),
        ("requirements.txt", "openai==0.28.1"),
    ]:
        out = run(ws, "edit_file", path=path, old_string=old, new_string=old + " ")
        assert not out.is_error, out.content


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("src/app.py", True),
        ("tests/test_x.py", True),
        ("requirements-dev.txt", True),
        ("pyproject.toml", True),
        ("setup.cfg", True),
        ("Pipfile", True),
        ("README.md", False),
        (".github/workflows/ci.yml", False),
        ("Makefile", False),
        ("notes.txt", False),
    ],
)
def test_editable(path, ok):
    assert editable(path) is ok


def test_unknown_tool_and_bad_arguments(ws):
    assert "unknown tool 'bash'" in run(ws, "bash", command="ls").content
    assert "unknown arguments" in run(ws, "read_file", path="app.py", mode="r").content
    assert "start_line must be int" in run(ws, "read_file", path="app.py", start_line="2").content
    out = run(ws, "edit_file", path="app.py", old_string=1, new_string="x")
    assert out.is_error and out.edit is None  # rejected before the edit was attempted


# --- argument validation (shared with tool calls parsed from text) ----------------------------


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ({"path": "a.py"}, None),
        ({"path": "a.py", "start_line": 2, "end_line": None}, None),
        ({}, "missing required arguments: ['path']"),
        ({"path": None}, "missing required arguments: ['path']"),
        ({"path": "a.py", "x": 1}, "unknown arguments: ['x']"),
        ({"path": 3}, "path must be string"),
        ({"path": "a.py", "start_line": True}, "start_line must be integer"),
        ({"path": "a.py", "start_line": 1.5}, "start_line must be integer"),
        ([], "tool input must be an object"),
    ],
)
def test_validate_tool_input(args, problem):
    (read_file,) = [t for t in TOOLS if t.name == "read_file"]
    assert validate_tool_input(read_file.input_schema, args) == problem


def test_edit_file_booleans_are_checked():
    (edit,) = [t for t in TOOLS if t.name == "edit_file"]
    base = {"path": "a.py", "old_string": "a", "new_string": "b"}
    assert validate_tool_input(edit.input_schema, base | {"replace_all": True}) is None
    assert validate_tool_input(edit.input_schema, base | {"replace_all": 1}) == (
        "replace_all must be boolean"
    )


def test_missing_required_argument_is_a_tool_error_not_a_crash(ws):
    out = run(ws, "read_file")
    assert out.is_error and "missing required arguments: ['path']" in out.content
    out = run(ws, "edit_file", path="app.py", old_string="x")
    assert out.is_error and "missing required arguments: ['new_string']" in out.content
