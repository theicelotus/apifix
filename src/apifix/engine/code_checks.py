"""Static checks on a model's rewrite of a file, before it is applied: names used but never
defined, and mock targets that point at names a repo module does not have or at the code the
test exercises. Both compare the candidate with the original file, so only what the rewrite
introduced is reported. Mock-target messages name the concrete target to patch instead when
the module's code reaches the migrated library in a recognisable way.

Pure functions (source text in, messages out); repo modules are read through a callback.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass

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


# --- patch sites: which test a patch applies to ---------------------------------------------


@dataclass(frozen=True)
class _PatchSite:
    line: int
    target: str
    function: str  # the patch function as written: "mock.patch", "patch", "mocker.patch", ...
    tests: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...]  # test functions it applies to


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
        if not (
            isinstance(node, ast.Call)
            and _dotted(node.func) in PATCH_FUNCTIONS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
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
        sites.append(_PatchSite(node.lineno, node.args[0].value, _dotted(node.func) or "", tests))
    return sorted(sites, key=lambda s: (s.line, s.target))


# --- what a module's code reaches the library through ---------------------------------------


@dataclass(frozen=True)
class _Suggestion:
    target: str  # the patch target, e.g. "app.openai.OpenAI"
    # what the test sets, e.g. "OpenAI.return_value.chat.completions.create.return_value"
    chain: str
    origin: str  # e.g. "ask() creates it with client = openai.OpenAI()"

    @property
    def var(self) -> str:
        return self.chain.split(".", 1)[0]


class _ModuleIndex:
    """A repo module's imports, top-level assignments and functions, to tell which calls go
    through the migrated library (`library_modules`)."""

    def __init__(self, dotted: str, tree: ast.Module, library_modules: frozenset[str]) -> None:
        self.dotted, self.library = dotted, library_modules
        self.imports: dict[str, str] = {}  # local name -> imported dotted path
        self.assigned: dict[str, ast.expr] = {}  # top-level name -> value
        self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.methods: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for stmt in _module_level(tree.body):
            if isinstance(stmt, ast.Import):
                for a in stmt.names:
                    if a.asname:
                        self.imports[a.asname] = a.name
                    else:
                        root = a.name.split(".")[0]
                        self.imports[root] = root
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

    def factory(self, value: ast.expr) -> tuple[str, str] | None:
        """("ctor", "openai.OpenAI") for a library call such as `openai.OpenAI()`, or
        ("factory", "get_client") for a call of a top-level function that returns one."""
        if not isinstance(value, ast.Call):
            return None
        func = _dotted(value.func)
        if self._from_library(func):
            return "ctor", func or ""
        fn = self.functions.get(func or "")
        if fn is not None:
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Return)
                    and isinstance(node.value, ast.Call)
                    and self._from_library(_dotted(node.value.func))
                ):
                    return "factory", fn.name
        return None

    def _suggest(self, made: tuple[str, str], rest: str, origin: str) -> _Suggestion:
        kind, what = made
        var = what.split(".")[-1]
        chain = f"{var}.return_value" + (f".{rest}.return_value" if rest else "")
        return _Suggestion(f"{self.dotted}.{what}", chain, origin)

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
                return _Suggestion(f"{self.dotted}.{dotted}", f"{attrs[-1]}.return_value", origin)
            if rest and self._from_library(dotted):
                origin = f"{function}() calls {dotted}()"
                return _Suggestion(f"{self.dotted}.{dotted}", f"{attrs[-1]}.return_value", origin)
        return None


def _example(s: _Suggestion, site: _PatchSite) -> str:
    """A 3-line example of the suggested patch, in the form the test used."""
    test = site.tests[0].name if site.tests else "test_example"
    value = "<the test's mock response>"
    if site.function == "mocker.patch":
        lines = [f'{s.var} = mocker.patch("{s.target}")', f"{s.chain} = {value}"]
    else:
        lines = [f'@{site.function}("{s.target}")', f"def {test}({s.var}):"]
        lines.append(f"    {s.chain} = {value}")
    return "For example:\n" + "\n".join(f"    {line}" for line in lines)


def _patch_text(s: _Suggestion) -> str:
    return f'patch("{s.target}") and set {s.chain}'


# --- the checks -------------------------------------------------------------------------------


def _missing_name_problem(
    site: _PatchSite, read_module: ReadModule, library_modules: frozenset[str]
) -> str | None:
    parts = site.target.split(".")
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
        head = (
            f"mock target {site.target!r}: {rel} has no top-level name {name!r} "
            f"(its top-level names: {shown or 'none'})"
        )
        index = _ModuleIndex(".".join(parts[:n]), ast.parse(text), library_modules)
        suggestion = index.where_created(name, ".".join(parts[n + 1 :]))
        if suggestion is None:
            return f"{head}. Patch a name that exists in {rel}"
        verb = "the constructor" if "creates" in suggestion.origin else "that"
        return (
            f"{head}; {suggestion.origin}. Patch {verb} instead: {_patch_text(suggestion)}.\n"
            + _example(suggestion, site)
        )
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
    library_modules: frozenset[str],
) -> str | None:
    """A patch of a repo function or class that the test it applies to calls directly."""
    parts = site.target.split(".")
    if len(parts) < 2 or not site.tests:
        return None
    found = _module_file(parts[:-1], read_module)
    if found is None:
        return None
    rel, text = found
    names = top_level_names(text)
    if names is None or parts[-1] not in names or _module_file(parts, read_module):
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
        index = _ModuleIndex(".".join(parts[:-1]), ast.parse(text), library_modules)
        suggestion = index.what_it_calls(parts[-1])
        if suggestion is None:
            return head
        return f"{head}: {suggestion.origin}, so {_patch_text(suggestion)}.\n" + _example(
            suggestion, site
        )
    return None


def mock_target_problems(
    original: str,
    candidate: str,
    read_module: ReadModule,
    *,
    library_modules: frozenset[str] = frozenset(),
) -> list[str]:
    """Mock targets new in the candidate that are wrong for a repo module (`read_module(relpath)`
    gives its current text, or None if there is no such file): a name the module does not
    have at top level, or the function or class the test the patch applies to calls directly
    (the code under test). With `library_modules` (the migrated library's top-level modules),
    a message names the concrete target to patch instead when the module's code reaches the
    library in a recognisable way."""
    try:
        tree = ast.parse(candidate)
    except (SyntaxError, ValueError):
        return []
    known = {target for _, target in mock_targets(original)}
    problems, seen = [], set()
    for site in _patch_sites(tree):
        if site.target in known or site.target in seen:
            continue
        problem = _missing_name_problem(site, read_module, library_modules)
        if problem is None:
            problem = _code_under_test_problem(site, tree, read_module, library_modules)
        if problem:
            seen.add(site.target)
            problems.append(problem)
    return problems
