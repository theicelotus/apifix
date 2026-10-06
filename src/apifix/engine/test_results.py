"""Turn test-runner output into counts and per-test outcomes.

Per-test IDs let the verifier spot tests that disappeared or became skipped after a fix, not
just a changed total. Runner output is untrusted (the repo's code produced it).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

Outcome = Literal["passed", "failed", "error", "skipped"]

MAX_REPORT_BYTES = 50_000_000
_SEVERITY = {"passed": 0, "skipped": 1, "error": 2, "failed": 3}


@dataclass(frozen=True)
class TestCaseResult:
    __test__ = False

    id: str
    outcome: Outcome


@dataclass(frozen=True)
class TestCounts:
    __test__ = False

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0


class ReportError(ValueError):
    pass


def counts_from(cases: list[TestCaseResult]) -> TestCounts:
    by = {o: sum(1 for c in cases if c.outcome == o) for o in _SEVERITY}
    return TestCounts(len(cases), by["passed"], by["failed"], by["skipped"], by["error"])


def _dedupe(cases: list[TestCaseResult]) -> list[TestCaseResult]:
    """Keep one result per id: the most severe (e.g. a pass plus a teardown error)."""
    best: dict[str, TestCaseResult] = {}
    for c in cases:
        if c.id not in best or _SEVERITY[c.outcome] > _SEVERITY[best[c.id].outcome]:
            best[c.id] = c
    return list(best.values())


def parse_junit(xml_text: str) -> tuple[TestCounts, list[TestCaseResult]]:
    """Parse pytest's --junitxml report."""
    if len(xml_text) > MAX_REPORT_BYTES:
        raise ReportError("junit report too large")
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise ReportError("junit report contains a DTD; refusing to parse")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ReportError(f"invalid junit xml: {exc}") from exc
    cases = []
    for tc in root.iter("testcase"):
        classname, name = tc.get("classname", ""), tc.get("name", "")
        tags = {child.tag for child in tc}
        if "failure" in tags:
            outcome: Outcome = "failed"
        elif "error" in tags:
            outcome = "error"
        elif "skipped" in tags:  # includes xfail
            outcome = "skipped"
        else:
            outcome = "passed"
        cases.append(TestCaseResult(f"{classname}::{name}" if classname else name, outcome))
    cases = _dedupe(cases)
    return counts_from(cases), cases


_UNITTEST_LINE = re.compile(
    r"^(?P<name>\w+) \((?P<where>[\w.]+)\)(?:\n[^\n]*?)? \.\.\. "
    r"(?P<status>ok|FAIL|ERROR|skipped\b.*|expected failure|unexpected success)$",
    re.M,
)
_UNITTEST_STATUS: dict[str, Outcome] = {
    "ok": "passed",
    "FAIL": "failed",
    "ERROR": "error",
    "expected failure": "skipped",
    "unexpected success": "failed",
}
_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_SUMMARY = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.M)


def parse_unittest_output(text: str) -> tuple[TestCounts, list[TestCaseResult]]:
    """Parse `python -m unittest -v` output (stderr). The summary lines are authoritative for
    counts; per-test lines are best effort (test output can interleave with them)."""
    cases = []
    for m in _UNITTEST_LINE.finditer(text):
        name, where, status = m.group("name"), m.group("where"), m.group("status")
        test_id = where if where.endswith(f".{name}") else f"{where}.{name}"
        outcome = "skipped" if status.startswith("skipped") else _UNITTEST_STATUS[status]
        cases.append(TestCaseResult(test_id, outcome))
    cases = _dedupe(cases)

    ran = _RAN.findall(text)
    summaries = _SUMMARY.findall(text)
    if not ran or not summaries:
        return counts_from(cases), cases
    total = int(ran[-1])
    fields = {}
    for part in (summaries[-1][1] or "").split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key.strip()] = int(value)
    failed = fields.get("failures", 0) + fields.get("unexpected successes", 0)
    errors = fields.get("errors", 0)
    skipped = fields.get("skipped", 0) + fields.get("expected failures", 0)
    passed = max(0, total - failed - errors - skipped)
    return TestCounts(total, passed, failed, skipped, errors), cases
