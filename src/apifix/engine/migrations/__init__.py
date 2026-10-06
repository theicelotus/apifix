"""Migration registry. Each migration is a package exposing a ``MIGRATION`` object."""

from __future__ import annotations

import ast
from typing import Protocol

from apifix.engine.dependencies import DeclaredDependency
from apifix.engine.models import Finding


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

    def check_python(self, file: str, tree: ast.Module, source: str) -> list[Finding]:
        """Return findings for one parsed Python file."""
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
