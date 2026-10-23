"""Is a repo already on the new API, with only its tests left behind?

Three things together say so: every declaration of the library requires the new version
(`Migration.first_version` or later), every finding is in a test file, and non-test code uses
names only the new API has (`Migration.new_api_uses`). Then the findings are stale tests, not a
migration, and `fix` stops before any LLM call or sandbox run. Pure stdlib, no LLM.
"""

from __future__ import annotations

import ast
import configparser
import io
import tokenize
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from apifix.engine.dependencies import (
    entirely_at_or_above,
    is_dependency_file,
    parse_dependency_file,
)
from apifix.engine.migrations import Migration
from apifix.engine.models import Finding
from apifix.engine.project import is_test_file
from apifix.engine.walk import iter_files

MAX_LISTED_USES = 5  # new-API uses shown in reports (the record keeps them all)


@dataclass(frozen=True)
class AlreadyMigrated:
    message: str  # "already on openai 1.x: the 8 findings are all in test files, ..."
    declarations: tuple[str, ...]  # "requirements.txt:37 openai==1.12.0"
    new_api_uses: tuple[str, ...]  # "src/app.py:23 openai.OpenAI"
    test_files: tuple[str, ...]  # the files holding the findings

    def report_lines(self) -> list[str]:
        uses = list(self.new_api_uses[:MAX_LISTED_USES])
        more = len(self.new_api_uses) - len(uses)
        return [
            f"Declared: {', '.join(self.declarations)}",
            f"New API in non-test code: {', '.join(uses)}" + (f" (+{more} more)" if more else ""),
            f"Test files with findings: {', '.join(self.test_files)}",
        ]


def detect_already_migrated(
    root: Path, migration: Migration, findings: Sequence[Finding]
) -> AlreadyMigrated | None:
    """The evidence that `root` (a directory) is already migrated and only its tests are stale,
    or None. Any dependency file that does not parse counts against it (its pin is unknown)."""
    first = getattr(migration, "first_version", None)
    uses_of = getattr(migration, "new_api_uses", None)
    if not findings or not root.is_dir() or first is None or not callable(uses_of):
        return None
    test_files = sorted({f.file for f in findings})
    if not all(is_test_file(f, root=root) for f in test_files):
        return None
    files = [(f, f.relative_to(root).as_posix()) for f in iter_files(root)]
    declarations = []
    for file, rel in files:
        if not is_dependency_file(rel):
            continue
        try:
            deps = parse_dependency_file(rel, file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError, configparser.Error):
            return None
        for dep in deps:
            if dep.name != migration.package:
                continue
            if not entirely_at_or_above(dep.clauses, first):
                return None
            declarations.append(f"{rel}:{dep.line} {dep.raw}")
    if not declarations:
        return None
    uses = []
    for file, rel in files:
        if file.suffix != ".py" or is_test_file(rel, root=root):
            continue
        tree = _parse(file)
        if tree is not None:
            uses += [f"{rel}:{line} {name}" for line, name in uses_of(rel, tree)]
    if not uses:
        return None
    n = len(findings)
    where = (
        "the finding is in a test file, which looks"
        if n == 1
        else f"the {n} findings are all in test files, which look"
    )
    return AlreadyMigrated(
        message=f"already on {migration.target_name}: {where} out of date with the code",
        declarations=tuple(declarations),
        new_api_uses=tuple(uses),
        test_files=tuple(test_files),
    )


def _parse(file: Path) -> ast.Module | None:
    try:
        data = file.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return ast.parse(data.decode(encoding), filename=str(file))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None
