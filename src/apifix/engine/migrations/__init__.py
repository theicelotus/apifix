"""Migration registry. Each migration is a package exposing a ``MIGRATION`` object."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from apifix.engine.dependencies import DeclaredDependency
from apifix.engine.models import Finding, SmokeCheck


class Migration(Protocol):
    name: str
    summary: str
    # Top-level modules of the migrated library: test edits that only rename references into
    # them are the migration itself, not test changes.
    api_modules: frozenset[str]
    # The library's catch-all exception classes; catching these in tests counts as broadening.
    broad_exceptions: frozenset[str]
    # Libraries whose types the new API uses (e.g. httpx for openai's error constructors): new
    # imports of them are not suspicious.
    companion_modules: frozenset[str]
    # Short old -> new reference (Markdown) for the fix agent's prompt.
    guide: str
    # Environment variables that get a placeholder value in the sandbox's test phase when the
    # repo's CI (GitHub Actions) sets them for its own runs. Literal values only, never secrets.
    ci_env_placeholders: Mapping[str, str]
    # (marker, explanation): when a failing test's output contains the marker, the retry
    # feedback carries the explanation.
    failure_hints: tuple[tuple[str, str], ...]
    # name -> the import that defines it, for names the library's 1.x code uses (apifix adds
    # a missing one when it is the only import known for the name).
    known_imports: Mapping[str, str]
    # call path (e.g. "chat.completions.create") -> an object-shaped example response for mocks
    mock_responses: Mapping[str, str]
    # Runs in the sandbox after the fixed code's dependencies are installed (no network, no repo
    # code); a failure is reported to the model as a dependency problem. None: no check.
    smoke_check: SmokeCheck | None

    def job_guide(self, findings: list[Finding], root: Path | None = None) -> tuple[str, list[str]]:
        """The parts of `guide` these findings need, and the ids of those parts. `root`: the
        repo, for recognising test files by content."""
        ...

    def check_python(
        self,
        file: str,
        tree: ast.Module,
        source: str,
        repo_modules: frozenset[str] = frozenset(),
    ) -> list[Finding]:
        """Return findings for one parsed Python file. `repo_modules`: dotted names of the
        scanned repo's own modules."""
        ...

    def check_dependency(self, file: str, dep: DeclaredDependency) -> Finding | None:
        """Return a finding if a declared dependency must change, else None."""
        ...


def _registry() -> dict[str, Migration]:
    from apifix.engine.migrations import openai_v1

    return {m.name: m for m in (openai_v1.MIGRATION,)}


def available_migrations() -> list[str]:
    return sorted(_registry())


def get_migration(name: str) -> Migration:
    try:
        return _registry()[name]
    except KeyError:
        known = ", ".join(available_migrations())
        raise ValueError(f"unknown migration {name!r} (available: {known})") from None
