"""Repo-level pass over the scanner's findings (Migration.adjust_findings).

`configure()` and the call sites it feeds often live in different files (a `config.py` sets the
key, `llm.py` builds the model); the scanner sees one file at a time, so a `client_config`
finding does not know where the calls are. This pass names those files on the finding so the fix
passes the same key into the `Client` built there.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from apifix.engine.models import Finding
from apifix.engine.project import is_test_file

_CONFIGURE_MARK = "genai.Client(api_key=...)"


def adjust_findings(findings: list[Finding], root: Path | None = None) -> list[Finding]:
    """`root`: the repo, for recognising test files by content."""
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
