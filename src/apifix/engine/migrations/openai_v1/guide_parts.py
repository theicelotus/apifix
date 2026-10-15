"""Which parts of guide.md a job's findings need (selectors in rules.py), and which rows of the
Resources table."""

from __future__ import annotations

import re
from pathlib import Path

from apifix.engine.migrations.guide import guide_parts, render_parts, with_sections
from apifix.engine.migrations.openai_v1 import rules
from apifix.engine.models import Finding
from apifix.engine.project import is_test_file

GUIDE_SOURCE = (Path(__file__).parent / "guide.md").read_text(encoding="utf-8")
PARTS = guide_parts(GUIDE_SOURCE)
_ROW = re.compile(r"^\| `openai\.(\w+)\.")


def _matches(finding: Finding, selector: tuple[frozenset[str], tuple[str, ...]]) -> bool:
    categories, keywords = selector
    if categories and finding.category not in categories:
        return False
    text = f"{finding.old_pattern}\n{finding.suggestion}"
    return not keywords or any(k in text for k in keywords)


def selected_parts(findings: list[Finding], root: Path | None = None) -> set[str]:
    """`root`: the repo, for recognising test files by content."""
    keep = set(rules.GUIDE_ALWAYS)
    if any(is_test_file(f.file, root=root) for f in findings):
        keep |= rules.GUIDE_FOR_TEST_FILES
    for part, selectors in rules.GUIDE_SELECTORS.items():
        if any(_matches(f, s) for f in findings for s in selectors):
            keep.add(part)
    return with_sections(keep)


def _resource_used(name: str, findings: list[Finding]) -> bool:
    path = rules.RESOURCES.get(name)
    word = re.compile(rf"\b{re.escape(name)}\b")
    client = re.compile(rf"(?<![\w.])client\.{re.escape(path)}(?!\w)") if path else None
    return any(
        word.search(f.old_pattern) or (client and client.search(f.suggestion)) for f in findings
    )


def _filter_resources(body: str, findings: list[Finding]) -> str:
    """Only the table rows for resources the findings use; no table if there are none."""
    lines = body.splitlines(keepends=True)
    rows = [i for i, line in enumerate(lines) if _ROW.match(line)]
    if not rows:
        return body
    kept = {i for i in rows if _resource_used(_ROW.match(lines[i]).group(1), findings)}  # type: ignore[union-attr]
    table = range(rows[0] - 2, rows[-1] + 1)  # header, separator, rows
    out = [
        line
        for i, line in enumerate(lines)
        if i not in table or (i in rows and i in kept) or (i not in rows and kept)
    ]
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def job_guide(findings: list[Finding], root: Path | None = None) -> tuple[str, list[str]]:
    """The guide text for these findings, and the ids of the parts it holds (in guide order)."""
    keep = selected_parts(findings, root)
    parts = [
        (part, _filter_resources(body, findings) if part == "resources" else body)
        for part, body in PARTS
    ]
    text = render_parts(parts, keep)
    return text, [part for part, _ in PARTS if part in keep]
