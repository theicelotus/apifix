"""Which parts of guide.md a job's findings need (selectors in rules.py)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from apifix.engine.migrations.google_genai import rules
from apifix.engine.migrations.guide import guide_parts, render_parts, with_sections
from apifix.engine.models import Finding
from apifix.engine.project import is_test_file

GUIDE_SOURCE = (Path(__file__).parent / "guide.md").read_text(encoding="utf-8")
PARTS = guide_parts(GUIDE_SOURCE)


def _matches(finding: Finding, selector: tuple[frozenset[str], tuple[str, ...]]) -> bool:
    categories, keywords = selector
    if categories and finding.category not in categories:
        return False
    text = f"{finding.old_pattern}\n{finding.suggestion}"
    return not keywords or any(k in text for k in keywords)


def selected_parts(
    findings: list[Finding], root: Path | None = None, shared: bool = False
) -> set[str]:
    """`root`: the repo, for recognising test files by content. `shared`: for a whole-file
    job's shared system prompt, which leaves out the per-file parts (rules.GUIDE_PER_FILE)."""
    keep = set(rules.GUIDE_ALWAYS)
    if any(is_test_file(f.file, root=root) for f in findings):
        keep |= rules.GUIDE_FOR_TEST_FILES
    for part, selectors in rules.GUIDE_SELECTORS.items():
        if any(_matches(f, s) for f in findings for s in selectors):
            keep.add(part)
    if shared:
        keep -= rules.GUIDE_PER_FILE
    return with_sections(keep)


def job_guide(
    findings: list[Finding], root: Path | None = None, shared: bool = False
) -> tuple[str, list[str]]:
    """The guide text for these findings, and the ids of the parts it holds (in guide order)."""
    keep = selected_parts(findings, root, shared)
    text = render_parts(PARTS, keep)
    return text, [part for part, _ in PARTS if part in keep]


def parts_text(part_ids: Iterable[str]) -> str:
    """The title and the parts `part_ids` (and their sections), in guide order."""
    return render_parts(PARTS, set(part_ids))
