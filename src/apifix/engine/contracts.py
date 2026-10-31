"""Test files that patch code a migration rewrites (whole-file mode).

A rewrite can change what a function returns (a `GenerativeModel` became a client), and a test
that patches that function, with no migration finding of its own, then feeds the old shape to
code that now expects the new one. The scanner never reports such a test, so it would never be
sent to the model. This finds them from the repo's files alone, no LLM:

- a `patch(...)` / `monkeypatch.setattr(...)` string target, or the `patch.object(module,
  "name")` form, whose last name is defined at the top level of a rewritten file, through
  that file's module or a repo module that imports the name from it (the usual "patch where it
  is used" target);
- also a `Class.method` target, through the class.

Pure `ast` and stdlib. Module names are matched by dotted suffix (`src.pkg.mod` answers to a
test's `pkg.mod`), like the scanner's string-reference matching.
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from apifix.engine.project import is_test_file
from apifix.engine.walk import iter_files

_PATCHERS = ("patch", "setattr", "object")


@dataclass(frozen=True)
class ContractPatch:
    test: str  # the test file
    line: int
    target: str  # the patch target as written ("pkg.mod.get_gemini")
    name: str  # the rewritten function or class it names ("get_gemini")
    defined_in: str  # the rewritten file that defines it

    def describe(self) -> str:
        return (
            f"{self.line}: patches `{self.target}`: `{self.name}` is defined in "
            f"{self.defined_in}, which this migration rewrites"
        )


def module_name(path: str) -> str:
    parts = path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None


def _defined(tree: ast.Module) -> set[str]:
    return {
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _imported_names(tree: ast.Module, own: str, is_package: bool) -> dict[str, str]:
    """Local name -> dotted `module.name` for every `from module import name` in the file
    (relative imports resolved against the file's own dotted name)."""
    out: dict[str, str] = {}
    package = own.split(".") if is_package else own.split(".")[:-1]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
            if node.level - 1 > len(package):
                continue
            module = ".".join([*base, *([node.module] if node.module else [])])
        else:
            module = node.module or ""
        for a in node.names:
            if a.name != "*" and module:
                out[a.asname or a.name] = f"{module}.{a.name}"
    return out


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted module, for `import a.b as c` and `from a import b`."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def _targets(tree: ast.Module) -> Iterable[tuple[int, str]]:
    """(line, dotted target) of every patch target in the file."""
    aliases = _aliases(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _PATCHERS:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            yield node.lineno, first.value
        elif (
            name == "object"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            module = _dotted(first, aliases)
            if module:
                yield node.lineno, f"{module}.{node.args[1].value}"


def contract_tests(
    repo: Path, rewritten: Iterable[str], skip: Collection[str] = ()
) -> dict[str, list[ContractPatch]]:
    """Test files under `repo`, by path, with the patches of names that the non-test Python
    files `rewritten` define. `skip`: files that are being sent anyway (not returned)."""
    defined: dict[str, set[str]] = {}
    for rel in rewritten:
        if not rel.endswith(".py") or is_test_file(rel, root=repo):
            continue
        tree = _parse(repo / rel)
        if tree is not None and (names := _defined(tree)):
            defined[rel] = names
    if not defined:
        return {}
    rewritten_modules = {module_name(rel): rel for rel in defined}

    def rewritten_for(dotted: str) -> str | None:
        """The rewritten file whose module `dotted` names (by dotted suffix)."""
        for module, rel in rewritten_modules.items():
            if dotted == module or module.endswith("." + dotted):
                return rel
        return None

    # which names each other non-test file imports from a rewritten file
    reexports: dict[str, dict[str, str]] = {}  # dotted module -> {name: defining rewritten file}
    tests: list[tuple[str, ast.Module]] = []
    for path in iter_files(repo):
        rel = path.relative_to(repo).as_posix()
        if path.suffix != ".py" or rel in skip or rel in defined:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        if is_test_file(rel, root=repo):
            tests.append((rel, tree))
            continue
        imported = _imported_names(tree, module_name(rel), rel.endswith("/__init__.py"))
        for local, dotted in imported.items():
            origin, _, name = dotted.rpartition(".")
            source = rewritten_for(origin)
            if source and name in defined[source]:
                reexports.setdefault(module_name(rel), {})[local] = source

    def defining_file(modpart: str, name: str) -> str | None:
        direct = rewritten_for(modpart)
        if direct and name in defined[direct]:
            return direct
        for module, names in reexports.items():
            if (module == modpart or module.endswith("." + modpart)) and name in names:
                return names[name]
        return None

    out: dict[str, list[ContractPatch]] = {}
    for rel, tree in tests:
        for line, target in _targets(tree):
            parts = target.split(".")
            for k in range(len(parts) - 1, 0, -1):  # pkg.mod.name, then pkg.mod.Class.method
                source = defining_file(".".join(parts[:k]), parts[k])
                if source:
                    patch = ContractPatch(rel, line, target, parts[k], source)
                    out.setdefault(rel, []).append(patch)
                    break
    return out
