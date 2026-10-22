"""Turn test-runner output into counts and per-test outcomes.

Per-test IDs let the verifier spot tests that disappeared or became skipped after a fix, not
just a changed total. Runner output is untrusted (the repo's code produced it).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Literal

Outcome = Literal["passed", "failed", "error", "skipped"]

MAX_REPORT_BYTES = 50_000_000
MAX_MESSAGE = 500
_SEVERITY = {"passed": 0, "skipped": 1, "error": 2, "failed": 3}


@dataclass(frozen=True)
class TestCaseResult:
    __test__ = False

    id: str
    outcome: Outcome
    message: str = ""  # start of the failure/error text; empty for passes and skips
    # the module in "No module named 'x'" anywhere in the full failure text (read before the
    # message is clipped: pytest prints it at the end of a traceback); "" if none
    missing_module: str = ""


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


def _clip(text: str) -> str:
    return text.strip()[:MAX_MESSAGE]


_MISSING_MODULE = re.compile(r"No module named ['\"]?([A-Za-z_][\w.]*)")


def missing_module(text: str) -> str:
    """The module of the last "No module named 'x'" in `text` (the error that ended the run,
    after any chained ones), or ""."""
    found = _MISSING_MODULE.findall(text)
    return found[-1] if found else ""


def _unittest_id(name: str, where: str) -> str:
    return where if where.endswith(f".{name}") else f"{where}.{name}"


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
        children = {child.tag: child for child in tc}
        if "failure" in children:
            outcome: Outcome = "failed"
        elif "error" in children:
            outcome = "error"
        elif "skipped" in children:  # includes xfail
            outcome = "skipped"
        else:
            outcome = "passed"
        message = missing = ""
        if outcome in ("failed", "error"):
            node = children["failure" if outcome == "failed" else "error"]
            full = f"{node.get('message', '')}\n{node.text or ''}"
            message, missing = _clip(full), missing_module(full)
        test_id = f"{classname}::{name}" if classname else name
        cases.append(TestCaseResult(test_id, outcome, message, missing))
    cases = _dedupe(cases)
    return counts_from(cases), cases


# A test's line under -v: `name (where)`, the first docstring line if it has one, ` ... `, then
# the status, either on the same line or (when the test logged to stderr in between) as the
# first line that is exactly a status, before the next test starts.
_UNITTEST_START = re.compile(r"^(?P<name>\w+) \((?P<where>[\w.]+)\)(?:\n[^\n]*?)? \.\.\. ", re.M)
_UNITTEST_STATUS_RE = re.compile(
    r"(?P<status>ok|FAIL|ERROR|skipped\b.*|expected failure|unexpected success)"
)
_UNITTEST_END = re.compile(r"^(?:={20,}|-{20,})$", re.M)
_UNITTEST_STATUS: dict[str, Outcome] = {
    "ok": "passed",
    "FAIL": "failed",
    "ERROR": "error",
    "expected failure": "skipped",
    "unexpected success": "failed",
}
_UNITTEST_BLOCK = re.compile(
    # an optional docstring line (-v prints the first one) before the dashes
    r"^(?:ERROR|FAIL): (?P<name>\w+) \((?P<where>[\w.]+)\)[^\n]*\n(?:[^\n]*\n)?-{20,}\n"
    r"(?P<body>.*?)"
    r"(?=^={20,}$|^-{20,}\nRan |\Z)",
    re.M | re.S,
)
_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
_SUMMARY = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.M)


def _unittest_lines(text: str) -> list[tuple[str, str, str]]:
    """(name, where, status) for each test line of -v output."""
    starts = list(_UNITTEST_START.finditer(text))
    out = []
    for i, m in enumerate(starts):
        bound = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        end = _UNITTEST_END.search(text, m.end(), bound)
        lines = text[m.end() : end.start() if end else bound].split("\n")
        # the rest of the start line, then whole lines of interleaved output
        for line in lines:
            status = _UNITTEST_STATUS_RE.fullmatch(line.rstrip("\r"))
            if status:
                out.append((m.group("name"), m.group("where"), status.group("status")))
                break
    return out


def parse_unittest_output(text: str) -> tuple[TestCounts, list[TestCaseResult]]:
    """Parse `python -m unittest -v` output (stderr). The summary lines are authoritative for
    counts; per-test lines are best effort (test output can interleave with them)."""
    bodies = {
        _unittest_id(m.group("name"), m.group("where")): m.group("body")
        for m in _UNITTEST_BLOCK.finditer(text)
    }
    cases = []
    for name, where, status in _unittest_lines(text):
        test_id = _unittest_id(name, where)
        outcome = "skipped" if status.startswith("skipped") else _UNITTEST_STATUS[status]
        body = bodies.get(test_id, "") if outcome in ("failed", "error") else ""
        cases.append(TestCaseResult(test_id, outcome, _clip(body), missing_module(body)))
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


# --- coverage -------------------------------------------------------------------------------


@dataclass(frozen=True)
class FileCoverage:
    executed: frozenset[int]
    missing: frozenset[int]

    @property
    def executable(self) -> frozenset[int]:
        return self.executed | self.missing


def parse_coverage_json(text: str, root: str = "/work") -> dict[str, FileCoverage]:
    """Parse `coverage json` output into {path relative to root: executed/missing lines}."""
    if len(text) > MAX_REPORT_BYTES:
        raise ReportError("coverage report too large")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReportError(f"invalid coverage json: {exc}") from exc
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise ReportError("coverage json has no 'files' table")
    out = {}
    prefix = root.rstrip("/") + "/"
    for path, info in files.items():
        if not isinstance(path, str) or not isinstance(info, dict):
            continue
        rel = path[len(prefix) :] if path.startswith(prefix) else path
        out[rel.removeprefix("./")] = FileCoverage(
            executed=frozenset(n for n in info.get("executed_lines", []) if isinstance(n, int)),
            missing=frozenset(n for n in info.get("missing_lines", []) if isinstance(n, int)),
        )
    return out
