"""Find every place in a repo affected by a migration.

Deterministic and offline: Python files are parsed with ``ast`` and dependency files with
stdlib parsers; the migration decides what is a finding. No LLM is involved.
"""

from __future__ import annotations

import ast
import configparser
import io
import os
import tokenize
from collections.abc import Iterable, Sequence
from pathlib import Path

from apifix.engine.already_migrated import detect_already_migrated
from apifix.engine.dependencies import is_dependency_file, parse_dependency_file
from apifix.engine.migrations import Migration, get_migration
from apifix.engine.models import Finding, ParseError, ScanResult
from apifix.engine.walk import iter_files


def scan(path: str | os.PathLike[str], migration: str | Migration) -> ScanResult:
    """Scan a file or directory. Symlinks are never followed."""
    if isinstance(migration, str):
        migration = get_migration(migration)
    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"no such file or directory: {root}")
    base = root.parent if root.is_file() else root
    result = ScanResult(migration=migration.name, root=str(root))
    files = list(iter_files(root))
    modules = repo_modules(f.relative_to(base).as_posix() for f in files)

    for file in files:
        rel = file.relative_to(base).as_posix()
        is_python = file.suffix == ".py"
        is_deps = is_dependency_file(rel)
        if not (is_python or is_deps):
            continue
        result.files_scanned += 1
        try:
            data = file.read_bytes()
        except OSError as exc:
            result.parse_errors.append(ParseError(rel, f"{type(exc).__name__}: {exc}"))
            continue
        if is_python:
            _scan_python(rel, data, migration, result, modules)
        if is_deps:
            _scan_dependencies(rel, data, migration, result)

    result.findings.sort()
    adjust = getattr(migration, "adjust_findings", None)
    if callable(adjust):
        result.findings = sorted(adjust(result.findings, base))
    if root.is_dir():
        result.already_migrated = detect_already_migrated(root, migration, result.findings)
    return result


def scan_text(
    rel: str,
    text: str,
    migration: Migration,
    modules: frozenset[str] = frozenset(),
    *,
    others: Sequence[Finding] = (),
    root: Path | None = None,
) -> list[Finding]:
    """The findings a scan of the repo at `root` reports for file `rel` if it holds `text`
    (written as UTF-8): the same per-file pass, with `modules` (repo_modules of the repo's
    files), then the migration's repo-level pass over these and `others` (the other files'
    findings; that pass only rewrites suggestions), keeping `rel`'s. [] for a file that does
    not parse."""
    result = ScanResult(migration=migration.name, root=str(root or rel))
    data = text.encode("utf-8")
    if rel.endswith(".py"):
        _scan_python(rel, data, migration, result, modules)
    if is_dependency_file(rel):
        _scan_dependencies(rel, data, migration, result)
    findings = sorted(result.findings)
    adjust = getattr(migration, "adjust_findings", None)
    if callable(adjust) and findings:
        everything = sorted([*(f for f in others if f.file != rel), *findings])
        findings = sorted(f for f in adjust(everything, root) if f.file == rel)
    return findings


def repo_modules(paths: Iterable[str]) -> frozenset[str]:
    """Dotted module names for the repo's Python files (`a/b.py` -> `a.b`, `a/__init__.py` ->
    `a`), as seen from the root."""
    out = set()
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            out.add(".".join(parts))
    return frozenset(out)


def _scan_python(
    rel: str,
    data: bytes,
    migration: Migration,
    result: ScanResult,
    modules: frozenset[str] = frozenset(),
) -> None:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        source = data.decode(encoding)
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
        result.parse_errors.append(ParseError(rel, f"{type(exc).__name__}: {exc}"))
        return
    result.findings.extend(migration.check_python(rel, tree, source, modules))


def _scan_dependencies(rel: str, data: bytes, migration: Migration, result: ScanResult) -> None:
    try:
        deps = parse_dependency_file(rel, data.decode("utf-8"))
    except (SyntaxError, UnicodeDecodeError, ValueError, configparser.Error) as exc:
        if rel.endswith(".py"):
            return  # already reported by the Python pass
        result.parse_errors.append(ParseError(rel, f"{type(exc).__name__}: {exc}"))
        return
    for dep in deps:
        finding = migration.check_dependency(rel, dep)
        if finding:
            result.findings.append(finding)
