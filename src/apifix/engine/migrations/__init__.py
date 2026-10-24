"""Migration registry. Each migration is a package exposing a ``MIGRATION`` object."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from apifix.engine.dependencies import DeclaredDependency
from apifix.engine.models import CompanionPin, Finding, SmokeCheck


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
    # The library's client classes and the resource attributes of their instances: a test that
    # patches a client class with autospec=True cannot use them (code_checks).
    client_classes: frozenset[str]
    client_resources: frozenset[str]
    # Runs in the sandbox after the fixed code's dependencies are installed (no network, no repo
    # code); a failure is reported to the model as a dependency problem. None: no check.
    smoke_check: SmokeCheck | None
    # The migrated library's distribution name, the version spec apifix writes for a pin its
    # dependency findings flag (no LLM involved), and, by normalised name, the library's own
    # requirements whose pins apifix raises in the same files when they allow none of the range.
    package: str
    pin_spec: str
    companion_pins: Mapping[str, CompanionPin]
    # The library's first version with the new API, and how reports name it ("openai 1.x"):
    # a repo whose declarations all require `first_version` or later, with findings only in
    # test files and the new API in its code, is already migrated (already_migrated.py).
    first_version: str
    target_name: str

    def job_guide(
        self, findings: list[Finding], root: Path | None = None, shared: bool = False
    ) -> tuple[str, list[str]]:
        """The parts of `guide` these findings need, and the ids of those parts. `root`: the
        repo, for recognising test files by content. `shared`: for a system prompt shared by
        every file's requests, which leaves out parts that go only with the files needing them
        (e.g. streaming); each file's request then adds those it needs."""
        ...

    def guide_text(self, part_ids: Iterable[str]) -> str:
        """The guide's title and the parts `part_ids` (for a request that needs parts the job's
        system prompt left out)."""
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

    def new_api_uses(self, file: str, tree: ast.Module) -> list[tuple[int, str]]:
        """(line, name) of every use of a name only the new API has, for one parsed file."""
        ...

    def adjust_findings(self, findings: list[Finding], root: Path | None = None) -> list[Finding]:
        """The scan's findings after a repo-level pass (called once at the end of a scan, on
        the sorted findings), e.g. suggestions that depend on other files. `root`: the scan
        root, for recognising test files by content."""
        ...

    def rewrite_checker(
        self, root: Path, findings: Sequence[Finding]
    ) -> Callable[[str, str, str], Mapping[str, list[str]]] | None:
        """Migration-specific checks of a rewritten file, built once per job from the original
        repo: a function (path, original, candidate) -> check name -> its errors (in order;
        every name each time), or None for no checks."""
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
