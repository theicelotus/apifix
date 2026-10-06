"""The fix agent's tools: read_file, list_files, search, edit_file. No shell.

All paths are relative to the agent's working copy of the repo. Tooling dirs (.git, venvs,
caches) and symlinks are invisible. Edits are exact string replacements in existing Python files
and dependency files only: no new files, no deletions. Outputs are bounded so a huge file
cannot flood the model's context.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from apifix.engine.dependencies import is_dependency_file
from apifix.engine.llm import ToolCall, ToolSpec
from apifix.engine.walk import SKIP_DIRS, iter_files

MAX_READ_LINES = 2000
MAX_READ_BYTES = 100_000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 300

TOOLS: list[ToolSpec] = [
    ToolSpec(
        "read_file",
        "Read a text file from the repository, with line numbers. Long files are cut off; "
        "use start_line/end_line to page through them.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the repo root."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "list_files",
        "List files under a directory of the repository (recursively), optionally filtered "
        "by a glob such as '*.py' or 'tests/**/*.py'.",
        {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Default: the repo root."},
                "glob": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "search",
        "Search file contents with a Python regular expression. Returns path:line: text.",
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string", "description": "Only files matching this glob."},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "edit_file",
        "Replace an exact string in an existing Python or dependency file. old_string must "
        "appear exactly once (include surrounding lines to make it unique) unless replace_all "
        "is true. Copy it from read_file output without the line-number prefix. Python files "
        "must still parse after the edit. Files cannot be created or deleted.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    ),
]
TOOL_NAMES = {t.name for t in TOOLS}


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class EditRecord:
    file: str
    old_string: str
    new_string: str
    replace_all: bool
    accepted: bool
    error: str = ""
    replacements: int = 0


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    edit: EditRecord | None = None
    duration_s: float = 0.0


@dataclass
class Workspace:
    """The agent's working copy; `root` is the only directory tools can touch."""

    root: Path
    edits: list[EditRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    # --- paths ----------------------------------------------------------------------------

    def resolve(self, rel: str, *, must_exist: bool = True) -> tuple[str, Path]:
        """(normalized relative path, absolute path) or ToolError."""
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("path is required")
        p = PurePosixPath(rel.strip().replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts:
            raise ToolError(f"{rel}: paths must be relative to the repo root, without '..'")
        parts = [x for x in p.parts if x != "."]
        if any(x in SKIP_DIRS or x.endswith(".egg-info") for x in parts):
            raise ToolError(f"{rel}: tooling directories are not accessible")
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ToolError(f"{rel}: symlinks are not followed")
        if must_exist and not current.exists():
            raise ToolError(f"{rel}: no such file or directory")
        return "/".join(parts) or ".", current

    # --- tools ----------------------------------------------------------------------------

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None):
        rel, file = self.resolve(path)
        if not file.is_file():
            raise ToolError(f"{rel}: not a file")
        text = _read_text(file, rel)
        lines = text.splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or len(lines), start + MAX_READ_LINES - 1)
        out, size = [], 0
        for n in range(start, end + 1):
            line = f"{n:6}\t{lines[n - 1]}"
            size += len(line) + 1
            if size > MAX_READ_BYTES:
                end = n - 1
                break
            out.append(line)
        header = f"{rel} (lines {start}-{end} of {len(lines)})"
        if end < len(lines):
            header += f"; more: read_file with start_line={end + 1}"
        return header + "\n" + "\n".join(out)

    def list_files(self, directory: str = ".", glob: str | None = None) -> str:
        rel, base = self.resolve(directory or ".")
        if not base.is_dir():
            raise ToolError(f"{rel}: not a directory")
        names = []
        for f in iter_files(base):
            name = f.relative_to(self.root).as_posix()
            if glob and not _glob_match(name, glob):
                continue
            names.append(name)
        if not names:
            return "(no files)"
        shown = names[:MAX_LIST_ENTRIES]
        more = len(names) - len(shown)
        return "\n".join(shown) + (f"\n... and {more} more (narrow with glob)" if more else "")

    def search(self, pattern: str, glob: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
        matches: list[str] = []
        for f in iter_files(self.root):
            name = f.relative_to(self.root).as_posix()
            if glob and not _glob_match(name, glob):
                continue
            try:
                if f.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if regex.search(line[:2000]):
                    matches.append(f"{name}:{n}: {_clip(line)}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        return "\n".join(matches) + "\n... more matches (narrow the search)"
        return "\n".join(matches) if matches else "(no matches)"

    def edit_file(
        self, path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> tuple[str, EditRecord]:
        def refuse(rel: str, why: str) -> ToolError:
            self.edits.append(
                EditRecord(rel, old_string, new_string, replace_all, accepted=False, error=why)
            )
            return ToolError(why)

        try:
            rel, file = self.resolve(path)
        except ToolError as exc:
            raise refuse(str(path), str(exc)) from None
        if not file.is_file():
            raise refuse(rel, f"{rel}: not a file (files cannot be created)")
        if not editable(rel):
            raise refuse(
                rel,
                f"{rel}: only existing Python files and dependency files (requirements*.txt, "
                "pyproject.toml, setup.py, setup.cfg, Pipfile) may be edited",
            )
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            raise refuse(rel, "old_string and new_string must be strings")
        if not old_string:
            raise refuse(rel, "old_string is empty (files cannot be created or rewritten whole)")
        if old_string == new_string:
            raise refuse(rel, "old_string and new_string are identical")
        try:
            text = _read_text(file, rel)
        except ToolError as exc:
            raise refuse(rel, str(exc)) from None
        count = text.count(old_string)
        if count == 0:
            raise refuse(rel, f"{rel}: old_string not found (copy it exactly from read_file)")
        if count > 1 and not replace_all:
            raise refuse(
                rel,
                f"{rel}: old_string appears {count} times; include more context or set replace_all",
            )
        new_text = text.replace(old_string, new_string)
        if rel.endswith(".py"):
            try:
                ast.parse(new_text)
            except SyntaxError as exc:
                raise refuse(
                    rel, f"{rel}: the edit would break the syntax (line {exc.lineno}: {exc.msg})"
                ) from None
        file.write_text(new_text, encoding="utf-8")
        record = EditRecord(rel, old_string, new_string, replace_all, True, "", count)
        self.edits.append(record)
        return f"{rel}: replaced {count} occurrence(s)", record

    # --- dispatch ---------------------------------------------------------------------------

    def run(self, call: ToolCall) -> ToolOutcome:
        start = time.monotonic()
        edit: EditRecord | None = None
        recorded = len(self.edits)
        try:
            args = _arguments(call)
            if call.name == "read_file":
                content = self.read_file(**args)
            elif call.name == "list_files":
                content = self.list_files(**args)
            elif call.name == "search":
                content = self.search(**args)
            elif call.name == "edit_file":
                content, edit = self.edit_file(**args)
            else:
                raise ToolError(f"unknown tool {call.name!r}; available: {sorted(TOOL_NAMES)}")
            outcome = ToolOutcome(content, edit=edit)
        except ToolError as exc:
            if len(self.edits) > recorded:  # a refused edit_file call
                edit = self.edits[-1]
            outcome = ToolOutcome(f"Error: {exc}", is_error=True, edit=edit)
        outcome.duration_s = time.monotonic() - start
        return outcome


def editable(rel: str) -> bool:
    """Python files (including tests) and dependency files; the verifier judges the rest."""
    return rel.endswith(".py") or is_dependency_file(rel)


_ARGS = {
    "read_file": {"path": str, "start_line": int, "end_line": int},
    "list_files": {"directory": str, "glob": str},
    "search": {"pattern": str, "glob": str},
    "edit_file": {"path": str, "old_string": str, "new_string": str, "replace_all": bool},
}


def _arguments(call: ToolCall) -> dict[str, Any]:
    spec = _ARGS.get(call.name)
    if spec is None:
        return {}
    if not isinstance(call.input, dict):
        raise ToolError("tool input must be an object")
    unknown = set(call.input) - set(spec)
    if unknown:
        raise ToolError(f"unknown arguments: {sorted(unknown)}")
    for key, value in call.input.items():
        kind = spec[key]
        if value is not None and (
            not isinstance(value, kind) or (kind is int and isinstance(value, bool))
        ):
            raise ToolError(f"{key} must be {kind.__name__}")
    return {k: v for k, v in call.input.items() if v is not None}


def _read_text(file: Path, rel: str) -> str:
    try:
        return file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{rel}: not a UTF-8 text file") from None
    except OSError as exc:
        raise ToolError(f"{rel}: {exc.strerror or exc}") from None


def _glob_match(path: str, glob: str) -> bool:
    """`*.py` matches at any depth; `tests/**/*.py` also matches `tests/x.py`."""
    candidates = {glob, glob.replace("**/", "")}
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g) for g in candidates)


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 3] + "..."
