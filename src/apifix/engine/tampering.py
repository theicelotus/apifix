"""Detect edits to tests that would make them easier to pass.

Old and new test files are compared by AST. Allowed silently: imports, mock.patch targets and
other dotted-path strings, and response-access rewrites (``r["choices"]`` -> ``r.choices``).
Other edits to test bodies, fixtures and helpers are allowed but reported as ``info`` so a
reviewer can look at them.
"""

from __future__ import annotations

import ast
import configparser
import contextvars
import copy
import json
import re
import tomllib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

Severity = Literal["reject", "warn", "info"]

SKIP_NAMES = {"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure"}
SKIP_CALLS = {"pytest.skip", "pytest.xfail", "pytest.importorskip", "self.skipTest"}
CONDITIONAL_SKIPS = {"skipif", "xfail", "skipIf", "skipUnless"}
BROAD_EXCEPTIONS = {"Exception", "BaseException", "AssertionError"}
COLLECTION_HOOKS = {
    "pytest_collection_modifyitems",
    "pytest_ignore_collect",
    "pytest_collect_file",
    "pytest_collection",
    "pytest_pycollect_makeitem",
    "pytest_configure",
    "pytest_runtest_setup",
    "pytest_runtest_call",
    "pytest_runtest_protocol",
    "pytest_runtest_makereport",
}
COLLECT_IGNORE = {"collect_ignore", "collect_ignore_glob"}
PYTEST_CONFIG_FILES = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}

# Assertion strength: 2 = exact (equality, identity, raises, called_with), 1 = partial
# (membership, ordering, isinstance), 0 = truthiness / not-None, -1 = always true.
_STRONG_ASSERTS = {
    "assertEqual",
    "assertEquals",
    "assertIs",
    "assertDictEqual",
    "assertListEqual",
    "assertTupleEqual",
    "assertSetEqual",
    "assertCountEqual",
    "assertSequenceEqual",
    "assertMultiLineEqual",
    "assertAlmostEqual",
    "assertRaises",
    "assertRaisesRegex",
    "assertWarns",
    "assertWarnsRegex",
    "assertLogs",
    "assert_called_once_with",
    "assert_called_with",
    "assert_has_calls",
    "assert_any_call",
    "assert_awaited_once_with",
    "assert_awaited_with",
    "raises",
    "warns",
}
_WEAK_ASSERTS = {"assertTrue", "assertFalse", "assertIsNotNone", "assertIsNone"}
_DOTTED_STRING = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)+$")


@dataclass(frozen=True)
class TestEdit:
    __test__ = False

    file: str
    line: int
    kind: str
    severity: Severity
    detail: str
    test: str | None = None  # qualified name in the file, e.g. "TestChat.test_answer"


# --- helpers --------------------------------------------------------------------------------


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return None


# Modules whose renamed attributes a migration may rewrite freely (e.g. "openai", so that
# `openai.error.RateLimitError` -> `openai.RateLimitError` is not a test change). Set per call
# of check_test_file.
_API_MODULES: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "api_modules", default=frozenset()
)
# Extra catch-all exception names for the migrated library (e.g. "APIError"), so narrowing
# `pytest.raises(RateLimitError)` to `pytest.raises(openai.APIError)` counts as broadening.
_API_BROAD: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "api_broad_exceptions", default=frozenset()
)


def _is_broad(node: ast.AST | None) -> bool:
    name = dotted_name(node) if node is not None else None
    if not name:
        return False
    return name in BROAD_EXCEPTIONS or name.rsplit(".", 1)[-1] in _API_BROAD.get()


class _Normalize(ast.NodeTransformer):
    """Erase differences a migration is allowed to make."""

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        name = dotted_name(node)
        if name and name.split(".", 1)[0] in _API_MODULES.get():
            return ast.Name(id="<api>", ctx=node.ctx)
        return self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        self.generic_visit(node)
        key = node.slice
        if (
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value.isidentifier()
        ):
            return ast.Attribute(value=node.value, attr=key.value, ctx=node.ctx)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, str) and _DOTTED_STRING.match(node.value):
            return ast.Constant("<dotted>")
        return node

    def visit_Import(self, node: ast.Import) -> None:
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return None


def _dump(node: ast.AST) -> str:
    normalized = _Normalize().visit(copy.deepcopy(node))
    return ast.dump(normalized, annotate_fields=False) if normalized else ""


def _source(source: str, node: ast.AST) -> str:
    text = ast.get_source_segment(source, node) or ""
    text = " ".join(text.split())
    return text if len(text) <= 100 else text[:97] + "..."


@dataclass
class _Test:
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    class_decorators: list[ast.expr]


def _is_test_class(node: ast.ClassDef) -> bool:
    bases = [dotted_name(b) or "" for b in node.bases]
    return node.name.startswith("Test") or any(b.endswith("TestCase") for b in bases)


def _collect_tests(tree: ast.Module) -> dict[str, _Test]:
    tests: dict[str, _Test] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            tests[node.name] = _Test(node.name, node, [])
        elif isinstance(node, ast.ClassDef) and _is_test_class(node):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and (
                    item.name.startswith("test")
                ):
                    qual = f"{node.name}.{item.name}"
                    tests[qual] = _Test(qual, item, list(node.decorator_list))
    return tests


# --- per-test facts -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Assertion:
    line: int
    strength: int
    constants: tuple[object, ...]
    text: str


def _compare_strength(test: ast.expr) -> int:
    if isinstance(test, ast.Constant):
        return -1 if test.value else 2
    if isinstance(test, ast.Compare):
        if all(isinstance(op, ast.Eq | ast.Is) for op in test.ops):
            return 2
        if any(isinstance(op, ast.IsNot) for op in test.ops) and any(
            isinstance(c, ast.Constant) and c.value is None for c in test.comparators
        ):
            return 0
        return 1
    if isinstance(test, ast.Call) and dotted_name(test.func) == "isinstance":
        return 1
    return 0


def _assert_call_strength(name: str, call: ast.Call) -> int | None:
    last = name.rsplit(".", 1)[-1]
    if name in ("pytest.raises", "pytest.warns") or last in _STRONG_ASSERTS:
        return 2
    if last in _WEAK_ASSERTS:
        if call.args and isinstance(call.args[0], ast.Constant):
            truthy = bool(call.args[0].value)
            if (last == "assertTrue" and truthy) or (last == "assertFalse" and not truthy):
                return -1
        return 0
    if last.startswith("assert"):
        return 1
    return None


def _constants(node: ast.AST) -> tuple[object, ...]:
    normalized = _Normalize().visit(copy.deepcopy(node))
    return tuple(
        n.value
        for n in ast.walk(normalized)
        if isinstance(n, ast.Constant) and n.value not in (None, True, False, "<dotted>")
    )


def _assertions(func: ast.AST, source: str) -> list[_Assertion]:
    found = []
    for node in ast.walk(func):
        strength = None
        if isinstance(node, ast.Assert):
            strength = _compare_strength(node.test)
        elif isinstance(node, ast.Call) and (name := dotted_name(node.func)):
            strength = _assert_call_strength(name, node)
        if strength is not None:
            found.append(_Assertion(node.lineno, strength, _constants(node), _source(source, node)))
    return sorted(found, key=lambda a: a.line)


def _skip_markers(decorators: list[ast.expr], body: ast.AST | None) -> list[tuple[str, str]]:
    """(name, normalized source) of every skip/xfail decorator and skip call."""
    marks = []
    for dec in decorators:
        name = dotted_name(dec) or ""
        if name.rsplit(".", 1)[-1] in SKIP_NAMES:
            marks.append((name.rsplit(".", 1)[-1], ast.dump(dec)))
    if body is not None:
        for node in ast.walk(body):
            if isinstance(node, ast.Call) and dotted_name(node.func) in SKIP_CALLS:
                marks.append((dotted_name(node.func) or "", ast.dump(node)))
            elif (
                isinstance(node, ast.Raise)
                and node.exc is not None
                and (dotted_name(node.exc) or "").endswith("SkipTest")
            ):
                marks.append(("SkipTest", ast.dump(node)))
    return marks


def _module_skips(tree: ast.Module) -> tuple[int, list[tuple[str, str]]]:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            values = (
                node.value.elts if isinstance(node.value, ast.List | ast.Tuple) else [node.value]
            )
            return node.lineno, _skip_markers(list(values), None)
    return 1, []


def _broad_handlers(func: ast.AST) -> int:
    count = 0
    for node in ast.walk(func):
        if isinstance(node, ast.ExceptHandler):
            types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            if node.type is None or any(_is_broad(t) for t in types):
                count += 1
        elif isinstance(node, ast.Call) and (dotted_name(node.func) or "").rsplit(".", 1)[-1] in (
            "raises",
            "assertRaises",
        ):
            if node.args and _is_broad(node.args[0]):
                count += 1
    return count


def _first_difference(old: ast.AST, new: ast.AST) -> int:
    """Line in `new` of the first top-level statement that differs after normalization."""
    old_body = getattr(old, "body", [])
    new_body = getattr(new, "body", [])
    for a, b in zip(old_body, new_body, strict=False):
        if _dump(a) != _dump(b):
            return b.lineno
    if len(new_body) > len(old_body):
        return new_body[len(old_body)].lineno
    return new.lineno


# --- comparisons ----------------------------------------------------------------------------


def _compare_test(file: str, old: _Test, new: _Test, old_src: str, new_src: str) -> list[TestEdit]:
    edits: list[TestEdit] = []
    q = new.qualname

    def add(line: int, kind: str, severity: Severity, detail: str) -> None:
        edits.append(TestEdit(file, line, kind, severity, detail, q))

    before, after = _assertions(old.node, old_src), _assertions(new.node, new_src)
    if len(after) < len(before):
        add(
            new.node.lineno, "assert_removed", "reject", f"{len(before)} -> {len(after)} assertions"
        )
    elif len(after) == len(before):
        for a, b in zip(before, after, strict=True):
            if b.strength < a.strength:
                add(b.line, "assert_weakened", "reject", f"`{a.text}` -> `{b.text}`")
            elif Counter(a.constants) - Counter(b.constants):
                add(b.line, "expected_value_changed", "warn", f"`{a.text}` -> `{b.text}`")

    old_skips = _skip_markers(old.class_decorators + old.node.decorator_list, old.node)
    new_skips = _skip_markers(new.class_decorators + new.node.decorator_list, new.node)
    if len(new_skips) > len(old_skips):
        add(new.node.lineno, "skip_added", "reject", "skip/xfail added")
    elif Counter(s for s in old_skips if s[0] in CONDITIONAL_SKIPS) != Counter(
        s for s in new_skips if s[0] in CONDITIONAL_SKIPS
    ):
        add(new.node.lineno, "skip_condition_changed", "warn", "skipif/xfail condition changed")

    if _broad_handlers(new.node) > _broad_handlers(old.node):
        add(new.node.lineno, "except_broadened", "reject", "broad except / raises(Exception) added")

    if not edits and _dump(old.node) != _dump(new.node):
        add(_first_difference(old.node, new.node), "setup_changed", "info", "test body changed")
    return edits


def _named_top_level(tree: ast.Module) -> dict[str, ast.stmt]:
    """Non-test definitions (fixtures, helpers, constants), keyed by name. Inside test classes
    these are the non-test members (setUp, class-level data), keyed "Class.member"."""
    return _named_statements(tree.body, set(_collect_tests(tree)), "")


def _named_statements(body: list[ast.stmt], tests: set[str], prefix: str) -> dict[str, ast.stmt]:
    out: dict[str, ast.stmt] = {}
    for i, node in enumerate(body):
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            name = f"{prefix}{node.name}"
            if name in tests:
                continue
            if isinstance(node, ast.ClassDef) and any(t.startswith(f"{name}.") for t in tests):
                out.update(_named_statements(node.body, tests, f"{name}."))
            else:
                out[name] = node
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [f"{prefix}{t.id}" for t in targets if isinstance(t, ast.Name)]
            out[",".join(names) or f"{prefix}<stmt {i}>"] = node
        else:
            out[f"{prefix}<stmt {i}>"] = node
    return out


def _hooks(tree: ast.Module) -> dict[str, str]:
    out = {}
    for name, node in _named_top_level(tree).items():
        if name in COLLECTION_HOOKS or set(name.split(",")) & COLLECT_IGNORE:
            out[name] = _dump(node)
    return out


def _hook_edits(file: str, old_tree: ast.Module, new_tree: ast.Module) -> list[TestEdit]:
    """Collection hooks / collect_ignore added or changed (conftest.py, usually)."""
    old_hooks = _hooks(old_tree)
    edits = []
    for name, dump in _hooks(new_tree).items():
        if old_hooks.get(name) != dump:
            node = _named_top_level(new_tree)[name]
            detail = f"{name} added or changed"
            edits.append(TestEdit(file, node.lineno, "test_config_changed", "reject", detail))
    return edits


def check_test_file(
    file: str,
    old_src: str | None,
    new_src: str | None,
    api_modules: frozenset[str] = frozenset(),
    api_broad_exceptions: frozenset[str] = frozenset(),
) -> list[TestEdit]:
    """Compare one test file across the fix. None means the file did not exist / was deleted.
    References into `api_modules` may be renamed freely (that is the migration), except that
    catching one of `api_broad_exceptions` counts as broadening."""
    modules = _API_MODULES.set(frozenset(api_modules))
    broad = _API_BROAD.set(frozenset(api_broad_exceptions))
    try:
        return _check_test_file(file, old_src, new_src)
    finally:
        _API_MODULES.reset(modules)
        _API_BROAD.reset(broad)


def _check_test_file(file: str, old_src: str | None, new_src: str | None) -> list[TestEdit]:
    try:
        old_tree = ast.parse(old_src) if old_src is not None else None
    except SyntaxError:
        old_tree = None  # was already broken: nothing to compare against
    if new_src is None:
        new_tree: ast.Module | None = None
    else:
        try:
            new_tree = ast.parse(new_src)
        except SyntaxError as exc:
            return [
                TestEdit(
                    file,
                    exc.lineno or 1,
                    "test_file_unparseable",
                    "reject",
                    f"SyntaxError: {exc.msg}",
                )
            ]
    if old_tree is None:
        # New test file (or the old one was unparseable): no test can have been weakened, but a
        # new conftest.py can still hide existing tests through collection hooks.
        empty = ast.Module(body=[], type_ignores=[])
        return _hook_edits(file, empty, new_tree) if new_tree is not None else []

    edits: list[TestEdit] = []
    old_tests = _collect_tests(old_tree)
    new_tests = _collect_tests(new_tree) if new_tree else {}

    # removed / renamed tests
    added = {q: t for q, t in new_tests.items() if q not in old_tests}
    for q, t in old_tests.items():
        if q in new_tests:
            continue
        body = _dump(ast.Module(body=t.node.body, type_ignores=[]))
        renamed = next(
            (
                n
                for n, nt in added.items()
                if _dump(ast.Module(body=nt.node.body, type_ignores=[])) == body
            ),
            None,
        )
        if renamed:
            edits.append(
                TestEdit(
                    file, added[renamed].node.lineno, "test_renamed", "info", f"{q} -> {renamed}", q
                )
            )
        else:
            edits.append(TestEdit(file, t.node.lineno, "test_removed", "reject", f"{q} removed", q))
    if new_tree is None:
        return edits

    for q in old_tests.keys() & new_tests.keys():
        edits += _compare_test(file, old_tests[q], new_tests[q], old_src or "", new_src or "")

    # module-level skips (pytestmark)
    old_line, old_marks = _module_skips(old_tree)
    new_line, new_marks = _module_skips(new_tree)
    if len(new_marks) > len(old_marks):
        edits.append(TestEdit(file, new_line, "skip_added", "reject", "pytestmark skip added"))
    elif Counter(new_marks) != Counter(old_marks):
        edits.append(
            TestEdit(
                file, new_line, "skip_condition_changed", "warn", "pytestmark condition changed"
            )
        )

    edits += _hook_edits(file, old_tree, new_tree)
    new_hooks = _hooks(new_tree)

    # fixtures, helpers, setup data
    old_named, new_named = _named_top_level(old_tree), _named_top_level(new_tree)
    for name, node in new_named.items():
        if name in new_hooks or name == "pytestmark":
            continue
        if name not in old_named or _dump(old_named[name]) != _dump(node):
            line = _first_difference(old_named[name], node) if name in old_named else node.lineno
            owner, _, member = name.rpartition(".")
            if not member.startswith("<"):
                label = name
            else:
                label = f"statement in {owner}" if owner else "module-level statement"
            edits.append(TestEdit(file, line, "setup_changed", "info", f"{label} changed"))
    return sorted(edits, key=lambda e: (e.line, e.kind))


# --- pytest configuration -------------------------------------------------------------------


def _pytest_section(name: str, text: str) -> str | None:
    """Serialized pytest settings from one config file (None if it has none)."""
    if name == "pyproject.toml":
        section = tomllib.loads(text).get("tool", {}).get("pytest")
        return json.dumps(section, sort_keys=True) if section is not None else None
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    header = "tool:pytest" if name == "setup.cfg" else "pytest"
    if not cfg.has_section(header):
        return None
    return json.dumps(sorted(cfg.items(header)))


def check_test_config(file: str, old_text: str | None, new_text: str | None) -> list[TestEdit]:
    name = PurePosixPath(file).name
    if name not in PYTEST_CONFIG_FILES:
        return []
    try:
        old = _pytest_section(name, old_text) if old_text is not None else None
        new = _pytest_section(name, new_text) if new_text is not None else None
    except (ValueError, configparser.Error) as exc:
        return [TestEdit(file, 1, "test_config_changed", "reject", f"unparseable: {exc}")]
    if old == new:
        return []
    line = 1
    for i, text_line in enumerate((new_text or "").splitlines(), 1):
        if re.match(r"^\s*\[(tool[.:]pytest[^\]]*|pytest)\]", text_line):
            line = i
            break
    return [TestEdit(file, line, "test_config_changed", "reject", "pytest configuration changed")]


def iter_rejects(edits: list[TestEdit]) -> Iterator[TestEdit]:
    return (e for e in edits if e.severity == "reject")
