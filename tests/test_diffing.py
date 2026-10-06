from __future__ import annotations

from apifix.engine.diffing import FileChange, changed_lines, diff_trees


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
        FileChange("new.py", "added", (1,), 0),
        FileChange("pkg/mod.py", "modified", (2,), 1),
    ]


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
