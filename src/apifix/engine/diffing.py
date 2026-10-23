"""Compare an original tree with a fixed tree: which files changed, and which lines."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from apifix.engine.walk import iter_files

ChangeKind = Literal["added", "deleted", "modified"]


@dataclass(frozen=True)
class Hunk:
    """One changed region of a text file. `old_start` is the original's first line (for an
    insertion: the line it follows); `new_start` the fixed file's (for a removal: the line
    after it). Both 1-based."""

    old_start: int
    new_start: int
    old: tuple[str, ...]
    new: tuple[str, ...]

    @property
    def new_lines(self) -> range:
        return range(self.new_start, self.new_start + len(self.new))


@dataclass(frozen=True)
class FileChange:
    path: str  # relative, POSIX separators
    kind: ChangeKind
    # 1-based line numbers in the FIXED file that were added or rewritten (empty for deletions
    # and binary files).
    changed_lines: tuple[int, ...] = ()
    removed_lines: int = 0  # lines of the original that are gone
    hunks: tuple[Hunk, ...] = ()  # text files that were modified or added

    @property
    def is_python(self) -> bool:
        return self.path.endswith(".py")


def read_text(path: Path) -> str | None:
    """The file's text with its line endings as they are (CRLF stays CRLF, so diffs apply)."""
    try:
        with path.open(encoding="utf-8", newline="") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _files(root: Path) -> dict[str, Path]:
    return {f.relative_to(root).as_posix(): f for f in iter_files(root)}


def diff_trees(original: Path, fixed: Path) -> list[FileChange]:
    before, after = _files(original), _files(fixed)
    changes = []
    for rel in sorted(before.keys() | after.keys()):
        old, new = before.get(rel), after.get(rel)
        if new is None:
            text = read_text(old) if old else None
            changes.append(FileChange(rel, "deleted", (), len(text.splitlines()) if text else 0))
            continue
        if old is not None and old.read_bytes() == new.read_bytes():
            continue
        old_text = read_text(old) if old is not None else ""
        new_text = read_text(new)
        kind: ChangeKind = "added" if old is None else "modified"
        if old_text is None or new_text is None:  # binary or undecodable
            changes.append(FileChange(rel, kind))
            continue
        changed, removed = changed_lines(old_text, new_text)
        changes.append(FileChange(rel, kind, changed, removed, hunks(old_text, new_text)))
    return changes


def opcodes(old: str, new: str) -> list[tuple[int, int, int, int]]:
    """(i1, i2, j1, j2) of every changed region, 0-based and half-open: the line diff that
    changed_lines, hunks and the unrelated-edit check share."""
    a, b = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [(i1, i2, j1, j2) for op, i1, i2, j1, j2 in matcher.get_opcodes() if op != "equal"]


def changed_lines(old: str, new: str) -> tuple[tuple[int, ...], int]:
    """(lines of `new` that were inserted or replaced, number of `old` lines removed)."""
    changed: list[int] = []
    removed = 0
    for i1, i2, j1, j2 in opcodes(old, new):
        changed.extend(range(j1 + 1, j2 + 1))
        removed += i2 - i1
    return tuple(changed), removed


def hunks(old: str, new: str) -> tuple[Hunk, ...]:
    a, b = old.splitlines(), new.splitlines()
    return tuple(
        Hunk(i1 + 1 if i2 > i1 else i1, j1 + 1, tuple(a[i1:i2]), tuple(b[j1:j2]))
        for i1, i2, j1, j2 in opcodes(old, new)
    )


def unified_diff(original: Path, fixed: Path, changes: list[FileChange] | None = None) -> str:
    """A `git apply`-able unified diff of the text files that changed."""
    out = []
    for ch in changes if changes is not None else diff_trees(original, fixed):
        old = read_text(original / ch.path) if ch.kind != "added" else ""
        new = read_text(fixed / ch.path) if ch.kind != "deleted" else ""
        if old is None or new is None:
            out.append(f"Binary files a/{ch.path} and b/{ch.path} differ\n")
            continue
        a = "/dev/null" if ch.kind == "added" else f"a/{ch.path}"
        b = "/dev/null" if ch.kind == "deleted" else f"b/{ch.path}"
        lines = list(
            difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), a, b)
        )
        for i, line in enumerate(lines):
            if not line.endswith("\n"):
                lines[i] = line + "\n\\ No newline at end of file\n"
        header = f"diff --git a/{ch.path} b/{ch.path}\n"
        if ch.kind == "added":
            header += "new file mode 100644\n"
        elif ch.kind == "deleted":
            header += "deleted file mode 100644\n"
        out.append(header + "".join(lines))
    return "".join(out)
