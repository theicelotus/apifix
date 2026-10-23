"""Third-party modules the repo imports but declares nowhere ("imported but not declared").

Read from the code before any sandbox run, so the report can list every such module at once
instead of one missing module per failing run. Information only: nothing is installed because
of it, and it never affects a verdict.

Stdlib only (the files are parsed, never imported).
"""

from __future__ import annotations

import ast
import configparser
import sys
from dataclasses import dataclass
from pathlib import Path

from apifix.engine.dependencies import (
    distribution_candidates,
    is_dependency_file,
    parse_dependency_file,
)
from apifix.engine.project import SandboxRequirements, build_install_plan
from apifix.engine.walk import iter_files, repo_module_names

# Installed in every sandbox (apifix's test runner, the build backend's usual tools), so
# importing them needs no declaration.
ALWAYS_INSTALLED = frozenset(
    {"pytest", "_pytest", "pluggy", "setuptools", "pkg_resources", "wheel", "pip", "coverage"}
)
# Top-level dirs whose Python files are not code the tests run (Sphinx conf.py imports themes).
DOC_DIRS = frozenset({"docs", "doc"})
# Namespace packages many distributions share: declared when any declared name has the prefix.
NAMESPACE_PREFIXES = {"google": "google-", "azure": "azure-"}
# Exception names whose handler makes an import optional.
_OPTIONAL_HANDLERS = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})
MAX_FILES_SHOWN = 5


@dataclass(frozen=True)
class UndeclaredImport:
    module: str  # the top-level import name as written, e.g. "vaderSentiment"
    distribution: str  # the likely pip name, normalised, e.g. "vadersentiment"
    files: tuple[str, ...]  # repo-relative files that import it, sorted


def undeclared_imports(
    root: Path, override: SandboxRequirements | None = None
) -> list[UndeclaredImport]:
    """Top-level third-party modules imported by the repo's Python files (code and tests)
    that no dependency file declares and `override` (--sandbox-requirements) does not list."""
    local = repo_module_names(root)
    declared = declared_names(root, override)
    found: dict[str, set[str]] = {}
    for path in iter_files(root):
        rel = path.relative_to(root)
        if path.suffix != ".py" or (len(rel.parts) > 1 and rel.parts[0] in DOC_DIRS):
            continue
        try:
            tree = ast.parse(path.read_bytes())
        except (SyntaxError, ValueError):
            continue
        for name in required_imports(tree):
            if name in sys.stdlib_module_names or name == "__future__":
                continue
            if name in local or name in ALWAYS_INSTALLED or _is_declared(name, declared):
                continue
            found.setdefault(name, set()).add(rel.as_posix())
    return [
        UndeclaredImport(name, _likely_distribution(name), tuple(sorted(files)))
        for name, files in sorted(found.items(), key=lambda kv: kv[0].lower())
    ]


def required_imports(tree: ast.Module) -> set[str]:
    """First names of the absolute imports in `tree`, except optional ones (inside a `try`
    whose handlers catch ImportError, ModuleNotFoundError, Exception or everything) and those
    under `if TYPE_CHECKING:`."""
    names: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
            return
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
            return
        if isinstance(node, ast.Try | ast.TryStar) and any(
            _optional_handler(h) for h in node.handlers
        ):
            for child in (*node.handlers, *node.orelse, *node.finalbody):
                visit(child)  # the fallback imports are required
            return
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                visit(child)
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return names


def declared_names(root: Path, override: SandboxRequirements | None = None) -> set[str]:
    """Normalised names of every distribution the repo declares anywhere (every dependency
    file the walker finds, extras and groups included; conda entries the install plan leaves
    out), plus the --sandbox-requirements lines."""
    names: set[str] = set()
    for path in iter_files(root):
        rel = path.relative_to(root).as_posix()
        if not is_dependency_file(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            names.update(d.name for d in parse_dependency_file(rel, text))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError, configparser.Error):
            continue
    plan = build_install_plan(root, None)
    names.update(plan.declared)
    names.update(n.name for n in plan.not_installed)
    if override is not None:
        lines = "\n".join(override.lines)
        names.update(d.name for d in parse_dependency_file("requirements.txt", lines))
    return names


def _is_declared(module: str, declared: set[str]) -> bool:
    if any(d in declared for d in distribution_candidates(module)):
        return True
    prefix = NAMESPACE_PREFIXES.get(module)
    return prefix is not None and any(d.startswith(prefix) for d in declared)


def _likely_distribution(module: str) -> str:
    candidates = distribution_candidates(module)
    return candidates[1] if len(candidates) > 1 else candidates[0]


def _optional_handler(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        (isinstance(t, ast.Name) and t.id in _OPTIONAL_HANDLERS)
        or (isinstance(t, ast.Attribute) and t.attr in _OPTIONAL_HANDLERS)
        for t in types
    )


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


# --- report -------------------------------------------------------------------------------------


def undeclared_section(items: list[UndeclaredImport]) -> list[str]:
    """The report's "Imported but not declared" section (empty when there is nothing)."""
    if not items:
        return []
    lines = [
        "## Imported but not declared",
        "",
        "Found by reading the code before any sandbox run. Information only: nothing was "
        "installed because of this list. To install them in the sandbox, list them in a "
        "`--sandbox-requirements` file.",
        "",
    ]
    for item in items:
        shown = ", ".join(item.files[:MAX_FILES_SHOWN])
        more = len(item.files) - MAX_FILES_SHOWN
        lines.append(
            f"- `{item.module}` (pip: `{item.distribution}`): {shown}"
            + (f" (+{more} more)" if more > 0 else "")
        )
    return [*lines, ""]


def undeclared_record(items: list[UndeclaredImport]) -> list[dict[str, object]]:
    """For trajectories."""
    return [
        {"module": i.module, "distribution": i.distribution, "files": list(i.files)} for i in items
    ]
