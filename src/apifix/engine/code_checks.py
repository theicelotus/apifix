"""Static checks on a model's rewrite of a file, before it is applied: names used but never
defined, and mock targets that point at names a repo module does not have or at the code the
test exercises. Both compare the candidate with the original file, so only what the rewrite
introduced is reported. Mock-target messages name the concrete target to patch instead when
the module's code reaches the migrated library in a recognisable way.

A patch of a library name (`patch("openai.OpenAI")`) is also checked against the repo's
non-test modules: one that binds the name at import time (`from openai import OpenAI`) never
sees it, so the message names the module's own target. A client class patched with
autospec=True is reported when the test uses a resource of its instance (`chat.completions`),
which an autospecced instance does not have. A patch of one of the library's resource methods
(`openai.resources.chat.completions.Completions.create`) leaves the client constructor running,
which is reported when the code under test builds a client without a key and the test sets none.

Pure functions (source text in, messages out); repo modules are read through a callback.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from pyflakes import checker, messages

from apifix.engine.context import reachable_modules
from apifix.engine.models import ClientKey

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
MAX_SUGGESTED_MODULES = 3  # library-using modules listed when the patched one has no library use
# Where repo modules are looked up, relative to the repo root; per test file, its own directory
# and the test plan's import paths come first (module_roots()).
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


ImportTable = Mapping[str, Sequence[str]]  # name -> the import lines that would define it


def import_table(*tables: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """name -> the distinct imports the tables offer for it (more than one: ambiguous)."""
    merged: dict[str, list[str]] = {}
    for table in tables:
        for name, line in table.items():
            if line not in merged.setdefault(name, []):
                merged[name].append(line)
    return {name: tuple(lines) for name, lines in merged.items()}


DEFAULT_IMPORTS = import_table(STDLIB_IMPORTS)


def _new_undefined(path: str, original: str, candidate: str) -> list[tuple[str, int]]:
    """(name, first line) of names undefined in the candidate but not in the original, in
    line order; [] when either version does not parse."""
    before, after = _undefined(path, original), _undefined(path, candidate)
    if before is None or after is None:
        return []
    return sorted(
        ((n, line) for n, line in after.items() if n not in before),
        key=lambda item: (item[1], item[0]),
    )


def undefined_names(
    path: str, original: str, candidate: str, known: ImportTable = DEFAULT_IMPORTS
) -> list[str]:
    """Names the candidate uses without defining them that the original did not already use
    that way (star imports and builtins are pyflakes' business: neither is reported). `known`
    imports are suggested in the message."""
    problems = []
    for name, line in _new_undefined(path, original, candidate):
        text = f"line {line}: {name!r} is used but never imported or defined"
        offered = known.get(name, ())
        if len(offered) == 1:
            text += f" (add: {offered[0]})"
        elif offered:
            text += f" (add one of: {'; '.join(offered)})"
        problems.append(text)
    return problems


def _import_insert_line(tree: ast.Module) -> int:
    """The 0-based line index after the module's docstring and its leading imports."""
    after = 0
    body = tree.body
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        after = first.end_lineno or first.lineno
        body = body[1:]
    for stmt in body:
        if not isinstance(stmt, ast.Import | ast.ImportFrom):
            break
        after = stmt.end_lineno or stmt.lineno
    return after


def add_missing_imports(
    path: str, original: str, candidate: str, known: ImportTable
) -> tuple[str, list[str]]:
    """The candidate with an import added for each new undefined name that has exactly one
    `known` import, after the module's leading imports; and the import lines added. Names
    with none or several known imports are left alone (the undefined-name check reports
    them)."""
    added = []
    for name, _ in _new_undefined(path, original, candidate):
        offered = known.get(name, ())
        if len(offered) == 1 and offered[0] not in added:
            added.append(offered[0])
    if not added:
        return candidate, []
    tree = ast.parse(candidate, filename=path)  # parses: _new_undefined found names
    newline = "\r\n" if "\r\n" in candidate else "\n"
    lines = candidate.splitlines(keepends=True)
    at = _import_insert_line(tree)
    if at and not lines[at - 1].endswith(("\n", "\r")):
        lines[at - 1] += newline
    lines[at:at] = [line + newline for line in added]
    return "".join(lines), added


# --- mock targets -----------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _string_target(node: ast.AST) -> str | None:
    """The target of a `patch("a.b.c")`-style call (PATCH_FUNCTIONS, string first argument)."""
    if (
        isinstance(node, ast.Call)
        and _dotted(node.func) in PATCH_FUNCTIONS
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value
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
        target = _string_target(node)
        if target is not None and isinstance(node, ast.Call):
            found.append((node.lineno, target))
    return sorted(found)


def module_roots(test_file: str, import_paths: Sequence[str] = ()) -> tuple[str, ...]:
    """Where a test file's bare imports and mock targets find repo modules: its own directory
    (pytest puts the test's directory, or the first non-package one above it, on sys.path),
    the test plan's import paths, the repo root and src/."""
    roots = []
    own = PurePosixPath(test_file).parent.as_posix()
    for d in (own, *import_paths):
        if d not in ("", "."):
            roots.append(d.rstrip("/") + "/")
    return tuple(dict.fromkeys((*roots, *MODULE_ROOTS)))


def _module_file(
    parts: list[str], read_module: ReadModule, roots: Sequence[str] = MODULE_ROOTS
) -> tuple[str, str] | None:
    """(relpath, text) of the repo module `parts` names, if any."""
    base = "/".join(parts)
    for root in roots:
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


# --- patch sites: which test a patch applies to ---------------------------------------------


@dataclass(frozen=True)
class _PatchSite:
    line: int
    target: str
    function: str  # the patch function as written: "mock.patch", "patch", "mocker.patch", ...
    tests: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]  # test functions it applies to
    text: str = ""  # the patch call as written (ast.unparse)


def _is_test_function(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _patch_sites(tree: ast.Module) -> list[_PatchSite]:
    """Every string-target patch, with the test functions it applies to: the decorated test
    (or a decorated class's test methods), or the test whose body contains it. Patches in
    fixtures, setUp, helpers or at module level apply to none."""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    sites = []
    for node in ast.walk(tree):
        target = _string_target(node)
        if target is None or not isinstance(node, ast.Call):
            continue
        tests: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...] = ()
        child, parent = node, parents.get(node)
        while parent is not None:
            if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                decorating = child in parent.decorator_list
                if isinstance(parent, ast.ClassDef):
                    if decorating:
                        tests = tuple(s for s in parent.body if _is_test_function(s))
                    break
                if _is_test_function(parent):
                    tests = (parent,)
                break
            child, parent = parent, parents.get(parent)
        sites.append(
            _PatchSite(node.lineno, target, _dotted(node.func) or "", tests, ast.unparse(node))
        )
    return sorted(sites, key=lambda s: (s.line, s.target))


# --- what a module's code reaches the library through ---------------------------------------


@dataclass(frozen=True)
class _Suggestion:
    target: str  # the patch target, e.g. "app.openai.OpenAI"
    # what the test sets, e.g. "OpenAI.return_value.chat.completions.create.return_value"
    chain: str
    origin: str  # e.g. "ask() creates it with client = openai.OpenAI()"
    path: str  # the call the response comes from, e.g. "chat.completions.create"

    @property
    def var(self) -> str:
        return self.chain.split(".", 1)[0]


class _ModuleIndex:
    """A repo module's imports, top-level assignments and functions, to tell which calls go
    through the migrated library (`library_modules`)."""

    def __init__(self, dotted: str, tree: ast.Module, library_modules: frozenset[str]) -> None:
        self.dotted, self.library = dotted, library_modules
        self.imports: dict[str, str] = {}  # local name -> imported dotted path
        self.module_names: dict[str, str] = {}  # local name -> module, of `import m [as a]`
        self.assigned: dict[str, ast.expr] = {}  # top-level name -> value
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for stmt in _module_level(tree.body):
            if isinstance(stmt, ast.Import):
                for a in stmt.names:
                    if a.asname:
                        self.imports[a.asname] = self.module_names[a.asname] = a.name
                    else:
                        root = a.name.split(".")[0]
                        self.imports[root] = self.module_names[root] = root
            elif isinstance(stmt, ast.ImportFrom) and stmt.module and not stmt.level:
                for a in stmt.names:
                    self.imports[a.asname or a.name] = f"{stmt.module}.{a.name}"
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                if isinstance(stmt.targets[0], ast.Name):
                    self.assigned[stmt.targets[0].id] = stmt.value
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions[stmt.name] = stmt
            elif isinstance(stmt, ast.ClassDef):
                self.methods += [
                    s for s in stmt.body if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)
                ]

    def _from_library(self, dotted: str | None) -> bool:
        if not dotted:
            return False
        imported = self.imports.get(dotted.split(".")[0])
        return imported is not None and imported.split(".")[0] in self.library

    def _library_class(self, dotted: str | None) -> bool:
        """A class of the library (`openai.OpenAI`), not one of its calls
        (`openai.chat.completions.create`, whose result is a response)."""
        return self._from_library(dotted) and (dotted or "").rsplit(".", 1)[-1][:1].isupper()

    def factory(self, value: ast.expr) -> tuple[str, str] | None:
        """("ctor", "openai.OpenAI") for a library class call such as `openai.OpenAI()`, or
        ("factory", "get_client") for a call of a top-level function that returns one."""
        if not isinstance(value, ast.Call):
            return None
        func = _dotted(value.func)
        if self._library_class(func):
            return "ctor", func or ""
        fn = self.functions.get(func or "")
        if fn is not None:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Call)
                    and self._library_class(_dotted(node.value.func))
                ):
                    return "factory", fn.name
        return None

    def _suggest(self, made: tuple[str, str], rest: str, origin: str) -> _Suggestion:
        kind, what = made
        var = what.split(".")[-1]
        chain = f"{var}.return_value" + (f".{rest}.return_value" if rest else "")
        return _Suggestion(f"{self.dotted}.{what}", chain, origin, rest)

    def where_created(self, name: str, rest: str) -> _Suggestion | None:
        """Where a function assigns `name` (missing at top level) from a library call."""
        for fn in [*self.functions.values(), *self.methods]:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == name
                ):
                    made = self.factory(node.value)
                    if made is not None:
                        how = "creates it with" if made[0] == "ctor" else "gets it from"
                        origin = f"{fn.name}() {how} {name} = {ast.unparse(node.value)}"
                        return self._suggest(made, rest, origin)
        return None

    def what_it_calls(self, function: str) -> _Suggestion | None:
        """The first call in `function` that goes through the library."""
        fn = self.functions.get(function)
        if fn is None:
            return None
        local: dict[str, tuple[tuple[str, str], ast.expr]] = {}
        constructors: set[int] = set()
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                made = self.factory(node.value)
                if made is not None:
                    local[node.targets[0].id] = (made, node.value)
                    constructors.add(id(node.value))
        calls = sorted(
            (n for n in ast.walk(fn) if isinstance(n, ast.Call) and id(n) not in constructors),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for call in calls:
            attrs: list[str] = []
            base: ast.expr = call.func
            while isinstance(base, ast.Attribute):
                attrs.insert(0, base.attr)
                base = base.value
            rest = ".".join(attrs)
            if isinstance(base, ast.Call) and rest:  # get_client().chat.completions.create()
                made = self.factory(base)
                if made is not None:
                    return self._suggest(made, rest, f"{function}() calls {ast.unparse(base)}")
                continue
            if not isinstance(base, ast.Name):
                continue
            root = base.id
            if root in local and rest:
                made, value = local[root]
                how = "creates" if made[0] == "ctor" else "gets"
                origin = f"{function}() {how} {root} = {ast.unparse(value)}"
                return self._suggest(made, rest, origin)
            dotted = f"{root}.{rest}" if rest else root
            if root in self.assigned and rest and self.factory(self.assigned[root]):
                origin = f"{function}() calls the module-level {root}"
                chain = f"{attrs[-1]}.return_value"
                return _Suggestion(f"{self.dotted}.{dotted}", chain, origin, dotted)
            if rest and self._from_library(dotted):
                origin = f"{function}() calls {dotted}()"
                chain = f"{attrs[-1]}.return_value"
                return _Suggestion(f"{self.dotted}.{dotted}", chain, origin, dotted)
        return None

    def library_use(self) -> _Suggestion | None:
        """The first call in the module's top-level functions (source order) that goes through
        the library, with a target that fits how the module binds the name: the library's own
        path when the module reaches it as a module (`import openai` + `openai.OpenAI()` ->
        `openai.OpenAI`), else the module's (`from openai import OpenAI` -> `app.OpenAI`, a
        module-level client -> `app.client.chat.completions.create`, a factory)."""
        for name in self.functions:
            s = self.what_it_calls(name)
            if s is None:
                continue
            root, _, tail = s.target[len(self.dotted) + 1 :].partition(".")
            module = self.module_names.get(root)
            if module is None or module.split(".")[0] not in self.library:
                return s
            return replace(s, target=f"{module}.{tail}" if tail else module)
        return None


@dataclass(frozen=True)
class _Options:
    library_modules: frozenset[str]
    mock_responses: Mapping[str, str]  # call path -> object-shaped example response
    dict_helpers: tuple[str, ...]  # the candidate's top-level functions returning a dict literal
    roots: tuple[str, ...] = MODULE_ROOTS  # where repo modules are looked up
    modules: tuple[str, ...] = ()  # the repo's non-test Python files (relpaths)
    test_files: tuple[str, ...] = ()  # the repo's test files (relpaths)
    path: str | None = None  # the test file being checked (where reachability starts)
    unmigrated: frozenset[str] = frozenset()  # modules still on the old API (no suggestions)


MAX_EXAMPLE_LINE = 88


def _response_for(path: str, responses: Mapping[str, str]) -> str | None:
    """The example response for the longest key that `path` is or ends with."""
    keys = [k for k in responses if path == k or path.endswith(f".{k}")]
    return responses[max(keys, key=len)] if keys else None


def _dict_helpers(tree: ast.Module) -> tuple[str, ...]:
    return tuple(
        stmt.name
        for stmt in tree.body
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(isinstance(n, ast.Return) and isinstance(n.value, ast.Dict) for n in ast.walk(stmt))
    )


def _assignment(chain: str, value: str) -> list[str]:
    """`chain = value`, split as `chain = Head(` / inner / `)` when too long."""
    line = f"{chain} = {value}"
    if len(line) <= MAX_EXAMPLE_LINE or "(" not in value or not value.endswith(")"):
        return [line]
    head, _, inner = value.partition("(")
    return [f"{chain} = {head}(", f"    {inner[:-1]}", ")"]


def _example(s: _Suggestion, site: _PatchSite, response: str | None) -> str:
    """A short example of the suggested patch, in the form the test used, with an
    object-shaped response when the migration knows the call's response shape."""
    test = site.tests[0].name if site.tests else "test_example"
    value = response or "<the test's mock response>"
    if site.function == "mocker.patch":
        lines = [f'{s.var} = mocker.patch("{s.target}")', *_assignment(s.chain, value)]
    else:
        lines = [f'@{site.function}("{s.target}")', f"def {test}({s.var}):"]
        lines += [f"    {line}" for line in _assignment(s.chain, value)]
    return "For example:\n" + "\n".join(f"    {line}" for line in lines)


def _objects_note(options: _Options) -> str:
    """Mock responses must be objects, naming the test file's helpers that return dicts."""
    helpers = options.dict_helpers
    if len(helpers) == 1:
        return (
            f"Mock responses must be objects, not dicts: {helpers[0]}() returns a dict; make "
            "it return an object, e.g. with SimpleNamespace."
        )
    if helpers:
        names = ", ".join(f"{h}()" for h in helpers)
        return (
            f"Mock responses must be objects, not dicts: {names} return dicts; make them return "
            "objects, e.g. with SimpleNamespace."
        )
    return "Mock responses must be objects, not dicts (1.x code uses attribute access)."


def _fix_text(s: _Suggestion, site: _PatchSite, options: _Options) -> str:
    """`patch(...) and set <chain>.`, the objects-not-dicts note, and the example."""
    response = _response_for(s.path, options.mock_responses)
    text = f'patch("{s.target}") and set {s.chain}.'
    if response:
        text += f" {_objects_note(options)}"
    return f"{text}\n{_example(s, site, response)}"


def _library_name(options: _Options) -> str:
    return ", ".join(sorted(options.library_modules))


def _library_users(
    own: tuple[str, _ModuleIndex], options: _Options, imports: _RepoImports
) -> tuple[list[tuple[str, _Suggestion]], list[str], bool]:
    """Where a patch would reach the library, for a patched module (`own`: its relpath and
    index) whose own code gives no target: (relpath, suggestion) of at most
    MAX_SUGGESTED_MODULES modules, the further ones of the same group, and whether the test
    reaches them. The patched module comes first when it calls the library; then the non-test
    modules the test reaches through imports, or, when it reaches none, all of them. Modules
    still on the old API (`options.unmigrated`) are left out: their calls are no 1.x target."""
    if not options.library_modules:
        return [], [], False
    own_rel, own_index = own
    first: list[tuple[str, _Suggestion]] = []
    own_use = own_index.library_use() if own_rel not in options.unmigrated else None
    if own_use is not None:
        first.append((own_rel, own_use))
    users: dict[str, _Suggestion] = {}
    for rel in options.modules:
        if rel == own_rel or rel in options.unmigrated or not _module_paths(rel, options.roots):
            continue
        tree = imports.tree(rel)
        if tree is None:
            continue
        index = _ModuleIndex(imports.module_path(rel)[0], tree, options.library_modules)
        use = index.library_use()
        if use is not None:
            users[rel] = use
    reached = []
    if options.path and users:
        reached = reachable_modules(options.path, imports.read_module, options.roots, set(users))
    group = first + [(rel, users[rel]) for rel in (reached or users)]
    shown = group[:MAX_SUGGESTED_MODULES]
    return shown, [rel for rel, _ in group[MAX_SUGGESTED_MODULES:]], bool(first or reached)


def _users_text(
    own: tuple[str, _ModuleIndex], site: _PatchSite, options: _Options, imports: _RepoImports
) -> str | None:
    """ "Patch <library> where a module ... uses it:", one line per module with its target and
    mock chain, the objects-not-dicts note, and an example for the first; None when no module
    gives a target."""
    shown, also, reached = _library_users(own, options, imports)
    if not shown:
        return None
    where = (
        "a module the test reaches"
        if reached
        else "a module (the test reaches none of them through its imports)"
    )
    lines = [f"Patch {_library_name(options)} where {where} uses it:"]
    lines += [f'    {rel}: {s.origin}: patch("{s.target}") and set {s.chain}.' for rel, s in shown]
    if also:
        lines.append(f"    (also: {', '.join(also)})")
    response = _response_for(shown[0][1].path, options.mock_responses)
    if response:
        lines.append(_objects_note(options))
    lines.append(_example(shown[0][1], site, response))
    return "\n".join(lines)


# --- the checks -------------------------------------------------------------------------------


def _missing_name_problem(
    site: _PatchSite, read_module: ReadModule, options: _Options, imports: _RepoImports
) -> str | None:
    parts = site.target.split(".")
    for n in range(len(parts) - 1, 0, -1):
        found = _module_file(parts[:n], read_module, options.roots)
        if found is None:
            continue
        rel, text = found
        name = parts[n]
        names = top_level_names(text)
        if (
            names is None
            or name in names
            or _module_file(parts[: n + 1], read_module, options.roots)
        ):
            return None
        listed = sorted(names)
        shown = ", ".join(listed[:MAX_LISTED_NAMES]) + (
            ", ..." if len(listed) > MAX_LISTED_NAMES else ""
        )
        head = (
            f"mock target {site.target!r}: {rel} has no top-level name {name!r} "
            f"(its top-level names: {shown or 'none'})"
        )
        index = _ModuleIndex(".".join(parts[:n]), ast.parse(text), options.library_modules)
        suggestion = index.where_created(name, ".".join(parts[n + 1 :]))
        if suggestion is None:
            elsewhere = _users_text((rel, index), site, options, imports)
            if elsewhere is None:
                return f"{head}. Patch a name that exists in {rel}"
            lib = _library_name(options)
            if not index.library_use():
                # none of its names reaches the library: the suggestion comes first, not a
                # list of them (genai-gptoss-6's model kept the wrong module after one)
                return (
                    f"mock target {site.target!r}: {rel} has no top-level name {name!r} and "
                    f"does not use {lib}. {elsewhere}"
                )
            return f"{head}. {elsewhere}"
        verb = "the constructor" if "creates" in suggestion.origin else "that"
        fix = _fix_text(suggestion, site, options)
        return f"{head}; {suggestion.origin}. Patch {verb} instead: {fix}"
    return None  # not a repo module: outside the repo, not checked


def _test_imports(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """Module-level `from m import n [as a]` (a -> "m.n") and `import m [as a]` (a -> "m";
    `import a.b` binds a -> "a")."""
    names: dict[str, str] = {}
    modules: dict[str, str] = {}
    for stmt in _module_level(tree.body):
        if isinstance(stmt, ast.ImportFrom) and stmt.module and not stmt.level:
            for a in stmt.names:
                names[a.asname or a.name] = f"{stmt.module}.{a.name}"
        elif isinstance(stmt, ast.Import):
            for a in stmt.names:
                if a.asname:
                    modules[a.asname] = a.name
                else:
                    modules[a.name.split(".")[0]] = a.name.split(".")[0]
    return names, modules


def _calls_target(
    test: ast.FunctionDef | ast.AsyncFunctionDef,
    target: str,
    names: dict[str, str],
    modules: dict[str, str],
) -> str | None:
    """How `test` calls `target` directly ("ask" or "app.ask"), if it does."""
    for node in ast.walk(test):
        if not isinstance(node, ast.Call):
            continue
        called = _dotted(node.func)
        if not called:
            continue
        root, _, rest = called.partition(".")
        if not rest and names.get(root) == target:
            return called
        if rest and root in modules and f"{modules[root]}.{rest}" == target:
            return called
    return None


def _code_under_test_problem(
    site: _PatchSite,
    tree: ast.Module,
    read_module: ReadModule,
    options: _Options,
    imports: _RepoImports,
) -> str | None:
    """A patch of a repo function or class that the test it applies to calls directly."""
    parts = site.target.split(".")
    if len(parts) < 2 or not site.tests:
        return None
    found = _module_file(parts[:-1], read_module, options.roots)
    if found is None:
        return None
    rel, text = found
    names = top_level_names(text)
    if names is None or parts[-1] not in names or _module_file(parts, read_module, options.roots):
        return None
    imported, modules = _test_imports(tree)
    for test in site.tests:
        called = _calls_target(test, site.target, imported, modules)
        if called is None:
            continue
        head = (
            f"mock target {site.target!r} in {test.name}: the test calls {called}() from {rel} "
            "directly, so patching it replaces the function being tested; patch what it calls "
            "instead"
        )
        index = _ModuleIndex(".".join(parts[:-1]), ast.parse(text), options.library_modules)
        suggestion = index.what_it_calls(parts[-1])
        if suggestion is None:
            elsewhere = _users_text((rel, index), site, options, imports)
            if elsewhere is None:
                return head
            lib = _library_name(options)
            return f"{head}; {called}() does not call {lib} directly. {elsewhere}"
        return f"{head}: {suggestion.origin}, so {_fix_text(suggestion, site, options)}"
    return None


# --- patch where it is used: library names bound at import time ------------------------------


def _type_checking(test: ast.expr) -> bool:
    return _dotted(test) in ("TYPE_CHECKING", "typing.TYPE_CHECKING")


def _runtime_module_level(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """_module_level without the bodies of `if TYPE_CHECKING:` (never run)."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _runtime_module_level(
                stmt.orelse if _type_checking(stmt.test) else stmt.body + stmt.orelse
            )
        elif isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
            yield from _runtime_module_level(stmt.body + stmt.orelse)
        elif isinstance(stmt, ast.With | ast.AsyncWith):
            yield from _runtime_module_level(stmt.body)
        elif isinstance(stmt, ast.Try | ast.TryStar):
            handlers = [s for h in stmt.handlers for s in h.body]
            yield from _runtime_module_level(stmt.body + handlers + stmt.orelse + stmt.finalbody)


def _from_imports(tree: ast.Module, target: str) -> list[tuple[str, str]]:
    """(bound name, the import as written) of each module-level `from M import X [as Y]` with
    `M.X == target` whose name the module uses."""
    loaded = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    found = []
    for stmt in _runtime_module_level(tree.body):
        if not isinstance(stmt, ast.ImportFrom) or stmt.level or not stmt.module:
            continue
        for a in stmt.names:
            if f"{stmt.module}.{a.name}" == target and (a.asname or a.name) in loaded:
                alias = f" as {a.asname}" if a.asname else ""
                found.append((a.asname or a.name, f"from {stmt.module} import {a.name}{alias}"))
    return found


def _module_paths(rel: str, roots: Sequence[str]) -> list[str]:
    """The dotted names a repo file can be imported by under `roots` (each root that holds it,
    in order; the repo root always does): `src/app/api.py` -> `src.app.api`, `app.api`."""
    found = []
    for root in (*roots, ""):
        if not rel.startswith(root):
            continue
        stem = rel[len(root) :]
        stem = stem[: -len("/__init__.py")] if stem.endswith("/__init__.py") else stem[:-3]
        parts = stem.split("/")
        if stem and all(p.isidentifier() for p in parts):
            found.append(".".join(parts))
    return list(dict.fromkeys(found))


def _imported_modules(tree: ast.Module) -> set[str]:
    """Dotted names a file imports: `import a.b` -> a.b; `from a.b import c` -> a.b, a.b.c
    (c may be a submodule)."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


class _RepoImports:
    """The repo's Python files as they are now (parsed once, when first needed), and the
    dotted names its test files and its other modules import."""

    def __init__(self, read_module: ReadModule, options: _Options) -> None:
        self.read_module, self.options = read_module, options
        self._trees: dict[str, ast.Module | None] = {}
        self._by_tests: set[str] | None = None
        self._by_modules: set[str] | None = None

    def tree(self, rel: str) -> ast.Module | None:
        if rel not in self._trees:
            text = self.read_module(rel)
            try:
                self._trees[rel] = ast.parse(text) if text is not None else None
            except (SyntaxError, ValueError):
                self._trees[rel] = None
        return self._trees[rel]

    def _imports(self, files: Sequence[str]) -> set[str]:
        out: set[str] = set()
        for rel in files:
            tree = self.tree(rel)
            if tree is not None:
                out |= _imported_modules(tree)
        return out

    def module_path(self, rel: str) -> tuple[str, bool]:
        """How the tests import `rel`: the first of its dotted names a test file imports, else
        the first another module imports (known: True), else the shortest (False)."""
        names = _module_paths(rel, self.options.roots)
        if self._by_tests is None:
            self._by_tests = self._imports(self.options.test_files)
        for name in names:
            if name in self._by_tests:
                return name, True
        if self._by_modules is None:
            self._by_modules = self._imports(self.options.modules)
        for name in names:
            if name in self._by_modules:
                return name, True
        return min(names, key=len), False


def _uses_attribute(tree: ast.Module, target: str) -> bool:
    """Does the code use `target` as an attribute of the library module (`openai.OpenAI`, also
    through `import openai as oa`)? A patch of it then reaches that code."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:  # import openai as oa -> oa.X is openai.X
                    aliases[a.asname] = a.name
                else:  # import openai.types binds openai
                    root = a.name.split(".")[0]
                    aliases[root] = root
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted:
                root, _, rest = dotted.partition(".")
                if root in aliases and f"{aliases[root]}.{rest}" == target:
                    return True
    return False


def _library_binding_problem(
    target: str,
    tree: ast.Module,
    patched: set[str],
    read_module: ReadModule,
    options: _Options,
    imports: _RepoImports,
) -> str | None:
    """`patch("openai.OpenAI")` when a non-test module binds OpenAI at import time with `from
    openai import OpenAI`: the patch replaces the library's attribute, which that module no
    longer reads. Fine: modules using `openai.OpenAI` through `import openai`, imports inside
    functions (run under the patch), modules whose own target the test already patches (by
    the path the tests import it by, when that is known), and test files (`tree`) that use
    `openai.OpenAI` themselves (the patch is for their own call)."""
    parts = target.split(".")
    if (
        len(parts) < 2
        or parts[0] not in options.library_modules
        or _module_file(parts[:1], read_module, options.roots) is not None
        or _uses_attribute(tree, target)
    ):
        return None
    # (rel, dotted module, bound name, import, a patch of it by another path)
    missed: list[tuple[str, str, str, str, str]] = []
    for rel in options.modules:
        module = imports.tree(rel)
        if module is None:
            continue
        paths = _module_paths(rel, options.roots)
        if not paths:  # not importable by a dotted name (e.g. a dir with a dash): no target
            continue
        for bound, written in _from_imports(module, target):
            dotted, known = imports.module_path(rel)
            accepted = [dotted] if known else paths
            if any(f"{p}.{bound}" in patched for p in accepted):
                continue
            other = next((f"{p}.{bound}" for p in paths if f"{p}.{bound}" in patched), "")
            missed.append((rel, dotted, bound, written, other))
    if not missed:
        return None
    rel, dotted, bound, written, other = missed[0]
    text = (
        f'patch("{target}") does not affect {rel}, which imports {parts[-1]} with "{written}"; '
        f'patch "{dotted}.{bound}" instead'
    )
    if other:
        text += f' ("{other}" names it by a path the tests do not import it by)'
    others = list(
        dict.fromkeys(d if b == bound else f"{d} (as {b})" for _, d, b, _, _ in missed[1:])
    )
    if others:
        text += f" (and likewise for: {', '.join(others)})"
    return text


# --- autospec on a patched client class ------------------------------------------------------

PATCH_OBJECT_FUNCTIONS = frozenset(f"{f}.object" for f in PATCH_FUNCTIONS - {"mocker.patch"})


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _injects(deco: ast.expr) -> bool:
    """Does this patch decorator pass the test a mock (no `new` given)?"""
    if not isinstance(deco, ast.Call) or _keyword(deco, "new") is not None:
        return False
    func = _dotted(deco.func)
    if func in PATCH_FUNCTIONS:
        return len(deco.args) < 2
    return func in PATCH_OBJECT_FUNCTIONS and len(deco.args) < 3


def _parameter(fn: ast.FunctionDef | ast.AsyncFunctionDef, slot: int) -> str | None:
    """The parameter a patch decorator's mock fills: slot 0 is the first after self/cls."""
    params = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
    if params and params[0] in ("self", "cls"):
        params = params[1:]
    return params[slot] if slot < len(params) else None


Scope = ast.Module | ast.FunctionDef | ast.AsyncFunctionDef


def _mock_names(
    call: ast.Call, tree: ast.Module, parents: Mapping[ast.AST, ast.AST]
) -> list[tuple[Scope, str]]:
    """(scope, name) of the mock a patch call gives: the parameter it fills as a decorator
    (lowest decorator first; on a class, in each test method, after the method's own patch
    decorators), `with patch(...) as m`, `m = mocker.patch(...)` or `m = patch(...).start()`
    (in the enclosing function, or the module)."""
    parent = parents.get(call)
    if isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        if call not in parent.decorator_list or not _injects(call):
            return []
        below = parent.decorator_list[parent.decorator_list.index(call) + 1 :]
        slot = sum(_injects(d) for d in below)
        if not isinstance(parent, ast.ClassDef):
            name = _parameter(parent, slot)
            return [(parent, name)] if name else []
        found = []
        for method in parent.body:
            if _is_test_function(method):
                assert isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
                own = sum(_injects(d) for d in method.decorator_list)
                name = _parameter(method, own + slot)
                if name:
                    found.append((method, name))
        return found
    target: ast.expr | None = None
    if isinstance(parent, ast.withitem) and parent.context_expr is call:
        target = parent.optional_vars
    else:
        if (
            isinstance(parent, ast.Attribute)
            and parent.attr == "start"
            and isinstance(parents.get(parent), ast.Call)
        ):
            call_node = parents[parent]
            assert isinstance(call_node, ast.Call)
            parent = parents.get(call_node)
        if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
            target = parent.targets[0]
    if not isinstance(target, ast.Name):
        return []
    scope: ast.AST | None = parents.get(call)
    while scope is not None and not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        scope = parents.get(scope)
    if scope is None:
        return [(tree, target.id)]
    assert isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef)
    return [(scope, target.id)]


def _return_value_of(node: ast.AST, name: str) -> bool:
    """Is `node` `<name>.return_value`?"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "return_value"
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    )


def _resource_use(
    scope: Scope, name: str, resources: Collection[str], parents: Mapping[ast.AST, ast.AST]
) -> tuple[int, str, str] | None:
    """(line, the expression, what the client lacks) of the first use of a client resource on
    the mock's instance in `scope`: `<name>.return_value.chat.completions`, also through a
    name assigned from `<name>.return_value`. None when the scope sets `<name>.return_value`
    itself (the instance is then the test's own object)."""
    instances = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            if any(_return_value_of(t, name) for t in node.targets):
                return None
            if (
                len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and _return_value_of(node.value, name)
            ):
                instances.add(node.targets[0].id)
    uses = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Attribute) or node.attr not in resources:
            continue
        base = node.value
        if _return_value_of(base, name) or (isinstance(base, ast.Name) and base.id in instances):
            outer = parents.get(node)
            shown, lacks = node, f"usable {node.attr}"
            if isinstance(outer, ast.Attribute) and outer.value is node:
                shown, lacks = outer, f"{node.attr}.{outer.attr}"
            uses.append((node.lineno, node.col_offset, ast.unparse(shown), lacks))
    if not uses:
        return None
    line, _, text, lacks = min(uses)
    return line, text, lacks


def _client_class(
    target: str, read_module: ReadModule, options: _Options, classes: Collection[str]
) -> bool:
    """Does `target` name one of the library's client classes: by its last name (any module
    path), or through a repo module's import (`from openai import OpenAI as Client`)?"""
    parts = target.split(".")
    if len(parts) < 2:
        return False
    if parts[-1] in classes:
        return True
    found = _module_file(parts[:-1], read_module, options.roots)
    if found is None:
        return False
    try:
        tree = ast.parse(found[1])
    except (SyntaxError, ValueError):
        return False
    imported = _ModuleIndex("", tree, options.library_modules).imports.get(parts[-1], "")
    return imported.split(".")[0] in options.library_modules and imported.split(".")[-1] in classes


def _autospec_uses(
    source: str,
    read_module: ReadModule,
    options: _Options,
    classes: Collection[str],
    resources: Collection[str],
) -> dict[str, list[tuple[str, int, str, str]]]:
    """target -> (scope name, line, expression, what the client lacks) for each scope that uses
    a client resource on the instance of a client class patched with autospec=True."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    found: dict[str, list[tuple[str, int, str, str]]] = {}
    calls = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Call) and _string_target(n)),
        key=lambda n: (n.lineno, n.col_offset),
    )
    for call in calls:
        autospec = _keyword(call, "autospec")
        if not (isinstance(autospec, ast.Constant) and autospec.value is True):
            continue
        target = _string_target(call) or ""
        if _keyword(call, "return_value") is not None or not _client_class(
            target, read_module, options, classes
        ):
            continue
        for scope, name in _mock_names(call, tree, parents):
            use = _resource_use(scope, name, resources, parents)
            if use is not None:
                where = scope.name if not isinstance(scope, ast.Module) else "the module"
                found.setdefault(target, []).append((where, *use))
    return found


def autospec_client_problems(
    original: str,
    candidate: str,
    read_module: ReadModule,
    *,
    client_classes: Collection[str],
    client_resources: Collection[str],
    library_modules: frozenset[str] = frozenset(),
    roots: Sequence[str] = MODULE_ROOTS,
) -> list[str]:
    """A client class patched with autospec=True (`patch("app.OpenAI", autospec=True)`, as a
    decorator, a context manager or mocker.patch) whose instance the test then uses for a
    resource (`OpenAI.return_value.chat.completions`): autospec builds the instance from the
    class, where the resources are cached properties, so the mock has no `chat.completions`.
    Only uses the original did not already have (same target and scope) are reported."""
    options = _Options(library_modules, {}, (), tuple(roots))
    before = _autospec_uses(original, read_module, options, client_classes, client_resources)
    after = _autospec_uses(candidate, read_module, options, client_classes, client_resources)
    problems = []
    for target, uses in after.items():
        old = {where for where, *_ in before.get(target, [])}
        new = [u for u in uses if u[0] not in old]
        if not new:
            continue
        where, line, text, lacks = new[0]
        others = list(dict.fromkeys(w for w, *_ in new[1:] if w != where))
        also = f" (and likewise in: {', '.join(others)})" if others else ""
        problems.append(
            f"mock target {target!r} is patched with autospec=True, and {where} uses {text} "
            f"(line {line}){also}: an autospecced 1.x client has no {lacks} (resources are "
            "cached properties), so drop autospec=True when patching the client class "
            f'(patch("{target}")); patching the method instead leaves the real client being '
            "built, so the test would also need a placeholder API key"
        )
    return problems


# --- a patched resource method while the code under test builds a client without a key -------

PYTEST_CONFIGS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
# Methods that run before a test of their class (unittest and pytest styles).
SETUP_METHODS = frozenset({"setUp", "setUpClass", "setup_method", "setup_class", "setup"})
# conftest.py hooks that run before any test.
SETUP_HOOKS = frozenset({"pytest_configure", "pytest_sessionstart"})


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def resource_method(target: str, library_modules: Collection[str], resources: str) -> str | None:
    """The client path a patch of a resource method stands for, under any module prefix:
    `openai.resources.chat.completions.Completions.create` -> `chat.completions.create`
    (`openai.resources.chat.Completions.create` too); None for any other target."""
    parts = target.split(".")
    for i, part in enumerate(parts[:-2]):
        if part not in library_modules or parts[i + 1] != resources:
            continue
        rest = parts[i + 2 :]
        if not rest[-2][:1].isupper() or any(p[:1].isupper() for p in rest[:-2]):
            return None
        modules, cls, method = rest[:-2], rest[-2], rest[-1]
        name = _snake(cls.removeprefix("Async"))
        if not modules or modules[-1] != name:
            modules = [*modules, name]
        return ".".join([*modules, method])
    return None


def _is_str(node: ast.AST | None, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _sets_key(node: ast.AST, key: ClientKey, passed: bool = True) -> bool:
    """Does the code under `node` set the key's environment variable (`monkeypatch.setenv`,
    `patch.dict(os.environ, {...})`, `os.environ[...] = `, `os.environ.setdefault` /
    `.update`), or, with `passed`, pass a key to a call (`api_key=` with a value other than
    None)?"""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = _dotted(n.func) or ""
            last = func.rsplit(".", 1)[-1]
            first = n.args[0] if n.args else None
            if last in ("setenv", "putenv", "setdefault") and _is_str(first, key.env):
                return True
            if func.endswith("patch.dict") or last == "update":
                values = n.args[1:] if func.endswith("patch.dict") else n.args
                if any(
                    isinstance(v, ast.Dict) and any(_is_str(k, key.env) for k in v.keys)
                    for v in values
                ) or any(k.arg == key.env for k in n.keywords):
                    return True
            if passed and any(
                k.arg == key.keyword
                and not (isinstance(k.value, ast.Constant) and k.value.value is None)
                for k in n.keywords
            ):
                return True
        elif isinstance(n, ast.Assign | ast.AnnAssign):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Subscript) and _is_str(t.slice, key.env) for t in targets):
                return True
    return False


def _fixture(node: ast.stmt) -> tuple[str, bool] | None:
    """(name, autouse) of a pytest fixture definition."""
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return None
    for deco in node.decorator_list:
        call = deco if isinstance(deco, ast.Call) else None
        func = _dotted(call.func if call else deco) or ""
        if func.rsplit(".", 1)[-1] != "fixture":
            continue
        name, autouse = node.name, False
        if call is not None:
            named = _keyword(call, "name")
            if isinstance(named, ast.Constant) and isinstance(named.value, str):
                name = named.value
            flag = _keyword(call, "autouse")
            autouse = isinstance(flag, ast.Constant) and flag.value is True
        return name, autouse
    return None


def _usefixtures(decorators: Sequence[ast.expr]) -> list[str]:
    """Fixture names of `@pytest.mark.usefixtures("a", "b")` marks."""
    names = []
    for deco in decorators:
        if isinstance(deco, ast.Call) and (_dotted(deco.func) or "").endswith("usefixtures"):
            names += [a.value for a in deco.args if isinstance(a, ast.Constant)]
    return [n for n in names if isinstance(n, str)]


def _pytestmark(body: Sequence[ast.stmt]) -> list[ast.expr]:
    """The marks of a module's or class's `pytestmark = mark` / `[mark, ...]`."""
    for stmt in body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            value = stmt.value
            return list(value.elts) if isinstance(value, ast.List | ast.Tuple) else [value]
    return []


class _KeySources:
    """Where a test file's tests can get a key: the test file and the conftest.py files above
    it (module-level code, setup hooks, fixtures), and pytest-env settings in the repo's pytest
    configuration."""

    def __init__(self, path: str, tree: ast.Module, read_module: ReadModule, key: ClientKey):
        self.key, self.tree = key, tree
        self.modules = [tree]
        parent = PurePosixPath(path).parent
        for d in [parent, *parent.parents]:
            rel = "conftest.py" if d.as_posix() in ("", ".") else f"{d.as_posix()}/conftest.py"
            text = read_module(rel)
            try:
                conftest = ast.parse(text) if text is not None else None
            except (SyntaxError, ValueError):
                conftest = None
            if conftest is not None:
                self.modules.append(conftest)
        configs = [read_module(c) or "" for c in PYTEST_CONFIGS]
        self.global_key = any(f"{key.env}=" in c for c in configs) or any(
            self._module_sets_key(m) for m in self.modules
        )

    def _module_sets_key(self, module: ast.Module) -> bool:
        for stmt in module.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                if stmt.name in SETUP_HOOKS and _sets_key(stmt, self.key, passed=False):
                    return True
            elif not isinstance(stmt, ast.ClassDef) and _sets_key(stmt, self.key, passed=False):
                return True
        return False

    def _fixtures(self, cls: ast.ClassDef | None) -> list[tuple[str, bool, ast.stmt]]:
        """(name, autouse, definition), nearest first: the class, the test file, conftests."""
        bodies = ([cls.body] if cls else []) + [m.body for m in self.modules]
        return [(*f, s) for body in bodies for s in body if (f := _fixture(s))]

    def test_has_key(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, cls: ast.ClassDef | None):
        if self.global_key or _sets_key(fn, self.key):
            return True
        if cls is not None and (
            any(_sets_key(d, self.key) for d in cls.decorator_list)
            or any(
                _sets_key(s, self.key)
                for s in cls.body
                if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef) and s.name in SETUP_METHODS
            )
        ):
            return True
        fixtures = self._fixtures(cls)
        wanted = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]]
        marks = [*fn.decorator_list, *_pytestmark(self.tree.body)]
        if cls is not None:
            marks += [*cls.decorator_list, *_pytestmark(cls.body)]
        wanted += _usefixtures(marks) + [name for name, autouse, _ in fixtures if autouse]
        seen: set[str] = set()
        while wanted:
            name = wanted.pop(0)
            if name in seen:
                continue
            seen.add(name)
            found = next((s for n, _, s in fixtures if n == name), None)
            if not isinstance(found, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _sets_key(found, self.key):
                return True
            wanted += [a.arg for a in [*found.args.posonlyargs, *found.args.args]]
        return False


@dataclass(frozen=True)
class KeylessClient:
    rel: str
    line: int
    call: str  # e.g. "openai.OpenAI"
    target: str  # the client class where the module looks it up, e.g. "app.openai.OpenAI"


def _keyless_client(
    rel: str, imports: _RepoImports, options: _Options, key: ClientKey
) -> KeylessClient | None:
    """The first client construction inside a function or method of `rel` that passes no key
    (no `api_key=`, no `**kwargs`). Module-level constructions are left out: they run at
    import, before any test can set a key."""
    tree = imports.tree(rel)
    if tree is None:
        return None
    index = _ModuleIndex("", tree, options.library_modules)
    calls = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = _dotted(node.func) or ""
            root, _, rest = func.partition(".")
            imported = index.imports.get(root, "")
            full = f"{imported}.{rest}" if rest else imported
            if (
                full.split(".")[0] not in options.library_modules
                or full.rsplit(".", 1)[-1] not in key.classes
            ):
                continue
            if any(k.arg in (key.keyword, None) for k in node.keywords):
                continue
            calls.append((node.lineno, func))
    if not calls:
        return None
    line, func = min(calls)
    return KeylessClient(rel, line, func, f"{imports.module_path(rel)[0]}.{func}")


def keyless_client(
    read_module: ReadModule,
    modules: Sequence[str],
    key: ClientKey,
    *,
    library_modules: frozenset[str],
    roots: Sequence[str] = MODULE_ROOTS,
) -> KeylessClient | None:
    """The first of `modules` (non-test files, in order) that builds a client without a key
    inside a function (see _keyless_client)."""
    options = _Options(library_modules, {}, (), tuple(roots), tuple(modules))
    imports = _RepoImports(read_module, options)
    return next((k for rel in modules if (k := _keyless_client(rel, imports, options, key))), None)


def unkeyed_method_patch_problems(
    original: str,
    candidate: str,
    read_module: ReadModule,
    key: ClientKey,
    *,
    library_modules: frozenset[str],
    roots: Sequence[str] = MODULE_ROOTS,
    modules: Sequence[str] = (),
    test_files: Sequence[str] = (),
    path: str | None = None,
) -> list[str]:
    """A new or changed patch of one of the library's resource methods
    (`openai.resources.chat.completions.Completions.create`, any module prefix) in a test that
    sets no key, while a non-test module the test file reaches through imports (`path`; the
    working copy's text) builds a client without one (`OpenAI()` in a function). The patch
    leaves the constructor running, which raises without the key, and code that catches the
    error never reaches the patched method ("Called 0 times"). A test gets a key from an
    environment patch in the test, its class or a fixture it uses (by parameter,
    usefixtures, autouse; in the test file or a conftest.py above it), module-level code or a
    setup hook of those files, pytest-env settings (`OPENAI_API_KEY=` in the pytest config),
    or a key passed to a call in the test. A test that also patches a client class needs none."""
    if path is None:
        return []
    try:
        tree = ast.parse(candidate)
    except (SyntaxError, ValueError):
        return []
    options = _Options(
        library_modules, {}, (), tuple(roots), tuple(modules), tuple(test_files), path
    )
    try:
        before = {s.text for s in _patch_sites(ast.parse(original))}
    except (SyntaxError, ValueError):
        before = set()
    sites = _patch_sites(tree)
    methods = [
        (site, client_path)
        for site in sites
        if site.text not in before
        and site.tests
        and (client_path := resource_method(site.target, library_modules, key.resources))
    ]
    if not methods:
        return []
    imports = _RepoImports(read_module, options)
    reached = reachable_modules(path, read_module, options.roots, set(modules))
    keyless = next(
        (k for rel in reached if (k := _keyless_client(rel, imports, options, key))), None
    )
    if keyless is None:
        return []
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    with_client = {
        id(fn) for s in sites if s.target.rsplit(".", 1)[-1] in key.classes for fn in s.tests
    }
    sources = _KeySources(path, tree, read_module, key)
    problems = []
    for target in dict.fromkeys(site.target for site, _ in methods):
        client_path = next(c for s, c in methods if s.target == target)
        tests = [fn for s, _ in methods if s.target == target for fn in s.tests]
        unkeyed = []
        for fn in dict.fromkeys(tests):
            cls = parents.get(fn)
            cls = cls if isinstance(cls, ast.ClassDef) else None
            if id(fn) not in with_client and not sources.test_has_key(fn, cls):
                unkeyed.append(fn.name)
        if not unkeyed:
            continue
        also = f" (and likewise in: {', '.join(unkeyed[1:])})" if unkeyed[1:] else ""
        var = keyless.call.rsplit(".", 1)[-1]
        problems.append(
            f"mock target {target!r} patches a method, so the real client is still built: "
            f"{keyless.rel}:{keyless.line} calls {keyless.call}() without {key.keyword}=, which "
            f"raises when {key.env} is not set (tests run without it, and the code's own "
            f"try/except may hide the error), and {unkeyed[0]} sets no key{also}. Set a "
            f'placeholder key in the test (@mock.patch.dict(os.environ, {{"{key.env}": "test"}}) '
            f'or monkeypatch.setenv("{key.env}", "test")), or patch the client class instead, '
            f'without autospec: patch("{keyless.target}") and set '
            f"{var}.return_value.{client_path}.return_value"
        )
    return problems


def mock_target_problems(
    original: str,
    candidate: str,
    read_module: ReadModule,
    *,
    library_modules: frozenset[str] = frozenset(),
    mock_responses: Mapping[str, str] | None = None,
    roots: Sequence[str] = MODULE_ROOTS,
    modules: Sequence[str] = (),
    test_files: Sequence[str] = (),
    path: str | None = None,
    unmigrated_modules: Collection[str] = (),
    client_classes: Collection[str] = (),
    client_resources: Collection[str] = (),
    client_key: ClientKey | None = None,
) -> list[str]:
    """Mock targets new in the candidate that are wrong for a repo module (`read_module(relpath)`
    gives its current text, or None if there is no such file): a name the module does not
    have at top level, or the function or class the test the patch applies to calls directly
    (the code under test). With `library_modules` (the migrated library's top-level modules),
    a message names the concrete target to patch instead when the module's code reaches the
    library in a recognisable way; its example sets an object-shaped response from
    `mock_responses` (call path -> example) when one matches the call. A library target
    (`openai.OpenAI`) is wrong when one of `modules` (the repo's non-test files) binds that
    name with `from openai import OpenAI` (see _library_binding_problem; `test_files` tell
    which module path the tests use).

    When the patched module's own code gives no target, the message lists where a patch would
    reach the library instead: modules that call it (the patched one first, then those the
    test file at `path` reaches through imports, else all), each with the target that fits how
    it binds the library, except `unmigrated_modules` (still on the old API).

    With `client_classes` and `client_resources` (the library's client classes and the
    resource attributes of their instances), a client class newly patched with autospec=True
    whose instance the test uses for a resource is reported too (autospec_client_problems).
    With `client_key`, so is a patched resource method in a test that sets no key while the
    code under test builds a client without one (unkeyed_method_patch_problems)."""
    try:
        tree = ast.parse(candidate)
    except (SyntaxError, ValueError):
        return []
    options = _Options(
        library_modules,
        mock_responses or {},
        _dict_helpers(tree),
        tuple(roots),
        tuple(modules),
        tuple(test_files),
        path,
        frozenset(unmigrated_modules),
    )
    known = {target for _, target in mock_targets(original)}
    sites = _patch_sites(tree)
    patched = {site.target for site in sites}
    imports = _RepoImports(read_module, options)
    problems, seen = [], set()
    for site in sites:
        if site.target in known or site.target in seen:
            continue
        problem = _missing_name_problem(site, read_module, options, imports)
        if problem is None:
            problem = _code_under_test_problem(site, tree, read_module, options, imports)
        if problem is None:
            problem = _library_binding_problem(
                site.target, tree, patched, read_module, options, imports
            )
        if problem:
            seen.add(site.target)
            problems.append(problem)
    if client_classes and client_resources:
        problems += autospec_client_problems(
            original,
            candidate,
            read_module,
            client_classes=client_classes,
            client_resources=client_resources,
            library_modules=library_modules,
            roots=roots,
        )
    if client_key is not None:
        problems += unkeyed_method_patch_problems(
            original,
            candidate,
            read_module,
            client_key,
            library_modules=library_modules,
            roots=roots,
            modules=modules,
            test_files=test_files,
            path=path,
        )
    return problems
