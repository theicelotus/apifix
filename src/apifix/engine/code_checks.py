"""Static checks on a model's rewrite of a file, before it is applied: names used but never
defined, and mock targets that point at names a repo module does not have. Both compare the
candidate with the original file, so only what the rewrite introduced is reported.

Pure functions (source text in, messages out); repo modules are read through a callback.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator

from pyflakes import checker, messages

# Imports suggested for common standard-library names a rewrite forgets to import.
STDLIB_IMPORTS = {
    **{n: f"from types import {n}" for n in ("SimpleNamespace", "ModuleType")},
    **{
        n: f"from unittest.mock import {n}"
        for n in (
            "Mock",
            "MagicMock",
            "AsyncMock",
            "NonCallableMock",
            "PropertyMock",
            "patch",
            "call",
            "ANY",
            "sentinel",
            "mock_open",
            "create_autospec",
        )
    },
    "mock": "from unittest import mock",
    **{n: f"from dataclasses import {n}" for n in ("dataclass", "field", "asdict", "replace")},
    **{n: f"from pathlib import {n}" for n in ("Path", "PurePath", "PurePosixPath")},
    **{
        n: f"from typing import {n}"
        for n in ("Any", "Optional", "Union", "Callable", "Iterable", "Iterator", "TypedDict")
    },
    **{n: f"from collections import {n}" for n in ("defaultdict", "OrderedDict", "namedtuple")},
    **{n: f"from functools import {n}" for n in ("partial", "wraps", "lru_cache")},
    **{
        n: f"import {n}"
        for n in ("json", "os", "re", "sys", "time", "asyncio", "logging", "datetime")
    },
}

PATCH_FUNCTIONS = frozenset({"patch", "mock.patch", "unittest.mock.patch", "mocker.patch"})
MAX_LISTED_NAMES = 20
# Where repo modules are looked up, relative to the repo root.
MODULE_ROOTS = ("", "src/")

ReadModule = Callable[[str], str | None]


# --- undefined names --------------------------------------------------------------------------


def _undefined(path: str, source: str) -> dict[str, int] | None:
    """pyflakes' undefined names -> first line used; None when the source does not parse."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError):
        return None
    found: dict[str, int] = {}
    for m in checker.Checker(tree, filename=path).messages:
        if isinstance(m, messages.UndefinedName):
            found.setdefault(m.message_args[0], m.lineno)
    return found


def undefined_names(path: str, original: str, candidate: str) -> list[str]:
    """Names the candidate uses without defining them that the original did not already use
    that way (star imports and builtins are pyflakes' business: neither is reported)."""
    before, after = _undefined(path, original), _undefined(path, candidate)
    if before is None or after is None:
        return []
    problems = []
    for name, line in sorted(after.items(), key=lambda item: (item[1], item[0])):
        if name in before:
            continue
        text = f"line {line}: {name!r} is used but never imported or defined"
        if name in STDLIB_IMPORTS:
            text += f" (add: {STDLIB_IMPORTS[name]})"
        problems.append(text)
    return problems


# --- mock targets -----------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def mock_targets(source: str) -> list[tuple[int, str]]:
    """(line, target) for each `patch("a.b.c")`-style call or decorator (patch, mock.patch,
    unittest.mock.patch, mocker.patch) whose first argument is a string literal."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _dotted(node.func) in PATCH_FUNCTIONS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append((node.lineno, node.args[0].value))
    return sorted(found)


def _module_file(parts: list[str], read_module: ReadModule) -> tuple[str, str] | None:
    """(relpath, text) of the repo module `parts` names, if any."""
    base = "/".join(parts)
    for root in MODULE_ROOTS:
        for rel in (f"{root}{base}.py", f"{root}{base}/__init__.py"):
            text = read_module(rel)
            if text is not None:
                return rel, text
    return None


def _bound_names(target: ast.AST) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Tuple | ast.List):
        for elt in target.elts:
            yield from _bound_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _module_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements at module level, including inside if/try/with/for/while blocks."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If | ast.For | ast.AsyncFor | ast.While):
            yield from _module_level(stmt.body + stmt.orelse)
        elif isinstance(stmt, ast.With | ast.AsyncWith):
            yield from _module_level(stmt.body)
        elif isinstance(stmt, ast.Try | ast.TryStar):
            handlers = [s for h in stmt.handlers for s in h.body]
            yield from _module_level(stmt.body + handlers + stmt.orelse + stmt.finalbody)


def top_level_names(source: str) -> set[str] | None:
    """Names a module defines at top level (def, class, assignment, import), plus names
    functions declare `global`. None when they cannot be known: the source does not parse, or
    it has a star import or a module-level `__getattr__`."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    names: set[str] = set()
    for stmt in _module_level(tree.body):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                names.update(_bound_names(target))
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign | ast.For | ast.AsyncFor):
            names.update(_bound_names(stmt.target))
        elif isinstance(stmt, ast.With | ast.AsyncWith):
            for item in stmt.items:
                if item.optional_vars is not None:
                    names.update(_bound_names(item.optional_vars))
        elif isinstance(stmt, ast.Import):
            names.update(a.asname or a.name.split(".")[0] for a in stmt.names)
        elif isinstance(stmt, ast.ImportFrom):
            if any(a.name == "*" for a in stmt.names):
                return None
            names.update(a.asname or a.name for a in stmt.names)
    if "__getattr__" in names:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    return names


def _target_problem(target: str, read_module: ReadModule) -> str | None:
    parts = target.split(".")
    for n in range(len(parts) - 1, 0, -1):
        found = _module_file(parts[:n], read_module)
        if found is None:
            continue
        rel, text = found
        name = parts[n]
        names = top_level_names(text)
        if names is None or name in names or _module_file(parts[: n + 1], read_module):
            return None
        listed = sorted(names)
        shown = ", ".join(listed[:MAX_LISTED_NAMES]) + (
            ", ..." if len(listed) > MAX_LISTED_NAMES else ""
        )
        return (
            f"mock target {target!r}: {rel} has no top-level name {name!r} "
            f"(its top-level names: {shown or 'none'}). Patch a name that exists in {rel}"
        )
    return None  # not a repo module: outside the repo, not checked


def mock_target_problems(original: str, candidate: str, read_module: ReadModule) -> list[str]:
    """Mock targets new in the candidate that name a repo module (`read_module(relpath)` gives
    its current text, or None if there is no such file) but not one of its top-level names."""
    known = {target for _, target in mock_targets(original)}
    problems, seen = [], set()
    for _, target in mock_targets(candidate):
        if target in known or target in seen:
            continue
        seen.add(target)
        problem = _target_problem(target, read_module)
        if problem:
            problems.append(problem)
    return problems
