"""Compact read-only context for a test file's request: instead of the full working copy of
each changed module, its changed top-level statements (with the library imports), or only its
*library uses*, the statements that touch the migrated library (how the module binds it and
which call chains it uses: what a test needs for mock targets). Modules the test reaches
through its imports come first.

Pure functions on source text; repo files are read through a callback.
"""

from __future__ import annotations

import ast
import difflib
from collections.abc import Callable, Collection, Iterable, Sequence
from pathlib import PurePosixPath

ReadModule = Callable[[str], str | None]
LIBRARY_USES_CHARS = 600  # per module
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(d.lineno for d in decorators)])  # type: ignore[attr-defined]


def _end(node: ast.AST) -> int:
    return node.end_lineno or node.lineno  # type: ignore[attr-defined]


def _parse(text: str | None) -> ast.Module | None:
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


# --- which modules a test reaches -------------------------------------------------------------


def _candidates(rel: str, node: ast.Import | ast.ImportFrom, roots: Sequence[str]) -> list[str]:
    """Repo paths an import statement in `rel` may name (modules, packages, submodules)."""
    names: list[list[str]] = []
    bases: list[str] = []
    if isinstance(node, ast.Import):
        names = [a.name.split(".") for a in node.names]
        bases = list(roots)
    elif node.level:
        here = PurePosixPath(rel).parent
        for _ in range(node.level - 1):
            here = here.parent
        base = "" if here.as_posix() == "." else here.as_posix() + "/"
        module = node.module.split(".") if node.module else []
        names = [module] if module else []
        names += [[*module, a.name] for a in node.names]
        bases = [base]
    elif node.module:
        module = node.module.split(".")
        names = [module, *([*module, a.name] for a in node.names)]
        bases = list(roots)
    out = []
    for base in bases:
        for parts in names:
            for i in range(1, len(parts) + 1):  # `import a.b.c` also imports a and a.b
                stem = base + "/".join(parts[:i])
                out += [f"{stem}.py", f"{stem}/__init__.py"]
    return out


def reachable_modules(
    start: str, read: ReadModule, roots: Sequence[str], wanted: Collection[str]
) -> list[str]:
    """The files in `wanted` that `start` reaches through imports of repo modules (absolute,
    under `roots`, and relative), in the order they are found."""
    seen = {start}
    queue = [start]
    found: list[str] = []
    while queue:
        rel = queue.pop(0)
        tree = _parse(read(rel))
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom):
                continue
            for cand in _candidates(rel, node, roots):
                if cand in seen or read(cand) is None:
                    continue
                seen.add(cand)
                queue.append(cand)
                if cand in wanted:
                    found.append(cand)
    return found


# --- changed statements -----------------------------------------------------------------------


def _units(tree: ast.Module) -> list[ast.stmt]:
    """Top-level statements, with classes split into their members."""
    out: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.body:
            out.append(node)  # the header, shown as its first line(s)
            out += node.body
        else:
            out.append(node)
    return out


def _is_library_import(node: ast.AST, library: Collection[str]) -> bool:
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] in library for a in node.names)
    return (
        isinstance(node, ast.ImportFrom)
        and bool(node.module)
        and not node.level
        and node.module.split(".")[0] in library  # type: ignore[union-attr]
    )


def _library_imports(tree: ast.Module, library: Collection[str]) -> list[ast.stmt]:
    return [node for node in tree.body if _is_library_import(node, library)]


def _text(lines: list[str], node: ast.stmt, header_only: bool = False) -> str:
    start, end = _start(node), _end(node)
    if header_only and isinstance(node, ast.ClassDef) and node.body:
        end = max(node.lineno, _start(node.body[0]) - 1)
    return f"# line {start}\n" + "".join(lines[start - 1 : end]).rstrip()


def changed_statements(original: str, current: str, library: Collection[str]) -> str:
    """The current text of every top-level statement (class member, for classes) that a
    change touches, with the module's library imports, in file order; each after a
    `# line N` comment. The full text when `current` does not parse."""
    tree = _parse(current)
    if tree is None:
        return current
    old, new = original.splitlines(keepends=True), current.splitlines(keepends=True)
    touched: set[int] = set()
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            touched.update(range(j1 + 1, max(j2, j1 + 1) + 1))
    chosen: dict[int, str] = {}
    for node in _library_imports(tree, library):
        chosen[_start(node)] = _text(new, node)
    for node in _units(tree):
        is_class = isinstance(node, ast.ClassDef)
        start = _start(node)
        end = max(node.lineno, _start(node.body[0]) - 1) if is_class else _end(node)
        hit = any(start <= n <= end for n in touched)
        if hit or (is_class and any(_start(m) <= n <= _end(m) for m in node.body for n in touched)):
            chosen[start] = _text(new, node, header_only=is_class)
    return "\n".join(chosen[k] for k in sorted(chosen))


# --- library uses -----------------------------------------------------------------------------


def _bound(tree: ast.Module, library: Collection[str]) -> tuple[set[str], set[str]]:
    """Names and `self.x` attributes bound to the library: its imports, then anything
    assigned from an expression that uses one (to a fixed point)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if _is_library_import(node, library):
            names |= {
                a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name)
                for a in node.names  # type: ignore[attr-defined]
            }
    attrs: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
                continue
            if not _uses(node.value, names, attrs):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id not in names:
                    names.add(t.id)
                    changed = True
                elif _self_attr(t) and _self_attr(t) not in attrs:
                    attrs.add(_self_attr(t))  # type: ignore[arg-type]
                    changed = True
    return names, attrs


def _self_attr(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    ):
        return node.attr
    return None


def _uses(node: ast.AST, names: set[str], attrs: set[str]) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id in names:
            return True
        if _self_attr(n) in attrs:
            return True
    return False


def _simple_statements(tree: ast.Module) -> Iterable[tuple[ast.stmt, str]]:
    """Every non-compound statement, with the qualified name of the def/class around it."""

    def walk(body: list[ast.stmt], scope: str) -> Iterable[tuple[ast.stmt, str]]:
        for node in body:
            if isinstance(node, (*_DEFS, ast.ClassDef)):
                yield from walk(node.body, f"{scope}.{node.name}" if scope else node.name)
            elif hasattr(node, "body"):  # if / for / while / with / try / match arms
                for name in ("body", "orelse", "finalbody"):
                    yield from walk(getattr(node, name, []) or [], scope)
                for handler in getattr(node, "handlers", []):
                    yield from walk(handler.body, scope)
            else:
                yield node, scope

    yield from walk(tree.body, "")


def library_uses(current: str, library: Collection[str], limit: int = LIBRARY_USES_CHARS) -> str:
    """The statements of a module that touch the library (its imports, and every simple
    statement using a name or self.x bound to it: its first line), each after a
    `# line N (in X)` comment; at most `limit` characters ("" when there are none)."""
    tree = _parse(current)
    if tree is None:
        return ""
    lines = current.splitlines(keepends=True)
    names, attrs = _bound(tree, library)
    out: list[str] = []
    for node in _library_imports(tree, library):
        out.append(_text(lines, node))
    for node, scope in _simple_statements(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if _uses(node, names, attrs):
            where = f" (in {scope})" if scope else ""
            body = lines[_start(node) - 1].strip()
            more = _end(node) - _start(node)
            if more:  # the call chain is on the first line; the arguments are not needed
                body += f"  # {more} more line{'s' if more > 1 else ''}"
            out.append(f"# line {_start(node)}{where}\n{body}")
    text = ""
    for entry in out:
        if len(text) + len(entry) + 1 > limit:
            return (text + "# ... (more not shown)").strip()
        text += entry + "\n"
    return text.rstrip("\n")
