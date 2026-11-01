from __future__ import annotations

import subprocess

from apifix.engine.diffing import (
    FileChange,
    Hunk,
    changed_lines,
    diff_trees,
    hunks,
    strip_added_trailing_whitespace,
    unified_diff,
)


def test_changed_lines():
    old = "a\nb\nc\nd\n"
    new = "a\nB\nc\nnew\nd\n"
    assert changed_lines(old, new) == ((2, 4), 1)
    assert changed_lines("a\nb\n", "a\n") == ((), 1)
    assert changed_lines("", "x\ny\n") == ((1, 2), 0)


def test_diff_trees(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    for root in (before, after):
        (root / "pkg").mkdir(parents=True)
        (root / "same.py").write_text("x = 1\n")
        (root / ".venv").mkdir()
    (before / "pkg" / "mod.py").write_text("a = 1\nb = 2\n")
    (after / "pkg" / "mod.py").write_text("a = 1\nb = 3\n")
    (before / "gone.py").write_text("x\ny\n")
    (after / "new.py").write_text("z = 1\n")
    (after / "blob.bin").write_bytes(b"\xff\xfe")
    (after / ".venv" / "ignored.py").write_text("junk\n")
    assert diff_trees(before, after) == [
        FileChange("blob.bin", "added"),
        FileChange("gone.py", "deleted", (), 2),
        FileChange("new.py", "added", (1,), 0, (Hunk(0, 1, (), ("z = 1",)),)),
        FileChange("pkg/mod.py", "modified", (2,), 1, (Hunk(2, 2, ("b = 2",), ("b = 3",)),)),
    ]


def test_hunks_number_both_sides():
    old = "a\nb\nc\nd\ne\n"
    new = "a\nB\nc\nnew\nd\n"
    assert hunks(old, new) == (
        Hunk(2, 2, ("b",), ("B",)),  # replaced
        Hunk(3, 4, (), ("new",)),  # inserted after original line 3
        Hunk(5, 6, ("e",), ()),  # removed; the fixed file's line 6 is where it was
    )
    assert [list(h.new_lines) for h in hunks(old, new)] == [[2], [4], []]


def test_unified_diff_applies_with_git(tmp_path):
    import shutil
    import subprocess

    import pytest

    from apifix.engine.diffing import unified_diff

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    original, fixed = tmp_path / "a", tmp_path / "b"
    for root, files in (
        (original, {"app.py": "x = 1\ny = 2\n", "req.txt": "openai==0.28.1", "gone.txt": "bye\n"}),
        (fixed, {"app.py": "x = 1\ny = 3\n", "req.txt": "openai>=1.0", "new.py": "z = 1\n"}),
    ):
        root.mkdir()
        for name, text in files.items():
            (root / name).write_text(text)
    patch = unified_diff(original, fixed)
    assert patch.startswith("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n")
    assert "\\ No newline at end of file" in patch
    work = tmp_path / "work"
    shutil.copytree(original, work)
    subprocess.run(["git", "apply", "-"], input=patch, text=True, cwd=work, check=True)
    assert {p.name: p.read_text() for p in work.iterdir()} == {
        p.name: p.read_text() for p in fixed.iterdir()
    }


def test_crlf_files_keep_their_line_endings_and_the_diff_applies(tmp_path):
    original, fixed = tmp_path / "a", tmp_path / "b"
    for root, body in ((original, "x = 1\r\ny = 2\r\n"), (fixed, "x = 1\r\ny = 3\r\n")):
        root.mkdir()
        (root / "app.py").write_bytes(body.encode())
    (change,) = diff_trees(original, fixed)
    assert change.changed_lines == (2,)
    diff = unified_diff(original, fixed)
    assert "-y = 2\r\n+y = 3\r\n" in diff
    subprocess.run(["git", "apply", "-"], input=diff.encode(), cwd=original, check=True)
    assert (original / "app.py").read_bytes() == (fixed / "app.py").read_bytes()


def test_strip_added_trailing_whitespace_only_touches_changed_lines(tmp_path):
    orig, fixed = tmp_path / "orig", tmp_path / "fixed"
    orig.mkdir()
    fixed.mkdir()
    (orig / "a.py").write_bytes(b"keep = 1   \r\nold = 2\r\n")
    (fixed / "a.py").write_bytes(b"keep = 1   \r\nnew = 2  \r\n\t\r\nlast = 3 \t")
    (orig / "b.md").write_text("x\n")
    (fixed / "b.md").write_text("x\nline  \n")
    (fixed / "c.txt").write_text("added  \nfile \n")
    (orig / "same.py").write_text("untouched   \n")
    (fixed / "same.py").write_text("untouched   \n")

    assert sorted(strip_added_trailing_whitespace(orig, fixed)) == ["a.py", "c.txt"]
    assert (fixed / "a.py").read_bytes() == b"keep = 1   \r\nnew = 2\r\n\r\nlast = 3"
    assert (fixed / "b.md").read_text() == "x\nline  \n"
    assert (fixed / "c.txt").read_text() == "added\nfile\n"
    assert (fixed / "same.py").read_text() == "untouched   \n"
    assert strip_added_trailing_whitespace(orig, fixed) == []
