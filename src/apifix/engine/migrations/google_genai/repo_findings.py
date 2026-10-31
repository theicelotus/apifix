"""Repo-level pass over the scanner's findings (Migration.adjust_findings).

`configure()` and the call sites it feeds often live in different files (a `config.py` sets the
key, `llm.py` builds the model); the scanner sees one file at a time, so a `client_config`
finding does not know where the calls are. This pass names those files on the finding so the fix
passes the same key into the `Client` built there.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from apifix.engine.imports import declared_names
from apifix.engine.migrations.google_genai import rules
from apifix.engine.models import CERTAIN, Finding
from apifix.engine.project import is_test_file

_CONFIGURE_MARK = "genai.Client(api_key=...)"


def _api_core(findings: list[Finding], root: Path | None) -> list[Finding]:
    """Decide the google.api_core findings (scanned in every file) from the repo.

    - Not a Gemini repo (no other finding, neither SDK declared): they are about another Google
      client, so drop them.
    - google-api-core is not declared (nor a distribution that depends on it): the migrated
      code has nothing that provides `google.api_core`, so they are certain, and the
      completeness check rejects a rewrite that still imports it.
    - Declared: the import keeps working; the exception findings stay likely (the rewrite may
      keep them for another client or for retries) and the import findings go."""
    core = [f for f in findings if rules.API_CORE_MARK in f.suggestion]
    if not core:
        return findings
    declared = declared_names(root) if root is not None else set()
    if len(core) == len(findings) and not declared & rules.GEMINI_DISTRIBUTIONS:
        return []
    kept = [f for f in findings if f not in core]
    if declared & rules.API_CORE_DEPENDENTS or any(
        d.startswith(rules.API_CORE_DEPENDENTS_PREFIXES) for d in declared
    ):
        return kept + [f for f in core if f.category != "import"]
    return kept + [replace(f, confidence=CERTAIN) for f in core]


def adjust_findings(findings: list[Finding], root: Path | None = None) -> list[Finding]:
    """`root`: the repo, for recognising test files by content."""
    findings = _api_core(findings, root)
    tests = {f.file for f in findings if is_test_file(f.file, root=root)}
    configures = [
        f
        for f in findings
        if f.category == "client_config" and _CONFIGURE_MARK in f.suggestion and f.file not in tests
    ]
    if not configures:
        return findings
    call_files = sorted(
        {
            f.file
            for f in findings
            if f.category == "api_call"
            and f.file not in tests
            and f.file not in {c.file for c in configures}
        }
    )
    if not call_files:
        return findings
    note = (
        f"; the calls that need this key are in {', '.join(call_files)}: pass the same value "
        "into the Client built there"
    )
    out = []
    for f in findings:
        if f in configures:
            out.append(replace(f, suggestion=f.suggestion + note))
        else:
            out.append(f)
    return out
