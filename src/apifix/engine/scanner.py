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
from pathlib import Path

from apifix.engine.dependencies import is_dependency_file, parse_dependency_file
from apifix.engine.migrations import Migration, get_migration
from apifix.engine.models import ParseError, ScanResult
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

    for file in iter_files(root):
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
            _scan_python(rel, data, migration, result)
        if is_deps:
            _scan_dependencies(rel, data, migration, result)

    result.findings.sort()
    return result


def _scan_python(rel: str, data: bytes, migration: Migration, result: ScanResult) -> None:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        source = data.decode(encoding)
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
        result.parse_errors.append(ParseError(rel, f"{type(exc).__name__}: {exc}"))
        return
    result.findings.extend(migration.check_python(rel, tree, source))


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
