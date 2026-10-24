"""Repo-level pass over the scanner's findings (Migration.adjust_findings).

The scanner sees one file at a time, so a call site's suggestion ("client = openai.OpenAI()")
knows nothing of a configured key in another module, and following it drops that key: 1.x's
`OpenAI()` never reads `openai.api_key`. When a non-test file keeps a configured value ("Never
drop" findings), call sites are pointed at the module-level client, which does read it.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from apifix.engine.migrations.openai_v1 import rules
from apifix.engine.models import Finding
from apifix.engine.project import is_test_file

_ASSIGNED = re.compile(r"\.(api_key|organization|api_base)\s*=")
_CALL = re.compile(
    r"^(?P<prefix>String reference \(e\.g\. a mock\.patch target\)[^:]*: )?Use "
    r"(?P<await>await )?client\.(?P<path>[\w.]+)\(\.\.\.\) with client = openai\."
    r"(?:Async)?OpenAI\(\); the response is a typed object, not a dict"
    rf"(?P<stream>{re.escape(rules.STREAM_CALL)})?$"
)
_NEW_NAME = {"api_key": "api_key", "organization": "organization", "api_base": "base_url"}


def _keeps_value(f: Finding) -> bool:
    return f.category == "client_config" and f.suggestion.startswith(
        (rules.CONFIG_KEEP_PREFIX, rules.CONFIG_API_BASE_PREFIX)
    )


def adjust_findings(findings: list[Finding], root: Path | None = None) -> list[Finding]:
    """`root`: the repo, for recognising test files by content."""
    tests = {f.file for f in findings if is_test_file(f.file, root=root)}
    kept = [f for f in findings if _keeps_value(f) and f.file not in tests]
    if not kept:
        return findings
    attrs = list(
        dict.fromkeys(_NEW_NAME[m.group(1)] for f in kept if (m := _ASSIGNED.search(f.old_pattern)))
    )
    if not attrs:
        return findings
    lines: dict[str, list[str]] = {}
    for f in kept:
        lines.setdefault(f.file, []).append(str(f.line))
    where = ", ".join(f"{file}:{','.join(numbers)}" for file, numbers in lines.items())
    reads = " and ".join(f"openai.{a}" for a in attrs)
    kwargs = ", ".join(f"{a}=openai.{a}" for a in attrs)
    call_files = sorted(
        {f.file for f in findings if f.category == "api_call" and f.file not in tests}
    )
    out = []
    for f in findings:
        m = _CALL.match(f.suggestion) if f.category == "api_call" else None
        if m and f.file in tests and m["prefix"]:
            mock = rules.MODULE_CLIENT_MOCK.format(path=m["path"])
            f = replace(f, suggestion=f"String reference (e.g. a mock.patch target): {mock}")
        elif m and f.file not in tests and not m["prefix"]:
            if m["await"]:
                text = rules.MODULE_CLIENT_ASYNC.format(path=m["path"], kwargs=kwargs)
            else:
                text = rules.MODULE_CLIENT_CALL.format(path=m["path"], attrs=reads, where=where)
            f = replace(f, suggestion=text + (m["stream"] or ""))
        elif f in kept and f.file not in call_files and call_files:
            keep = (
                "keep the value set here (as openai.base_url)"
                if f.suggestion.startswith(rules.CONFIG_API_BASE_PREFIX)
                else "keep this assignment"
            )
            files = ", ".join(call_files)
            text = rules.CONFIG_CALLS_ELSEWHERE.format(files=files, keep=keep)
            f = replace(f, suggestion=f.suggestion + text)
        out.append(f)
    return out
