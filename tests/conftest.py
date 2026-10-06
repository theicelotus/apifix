from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apifix.engine.models import Finding
from apifix.engine.scanner import scan

FIXTURES = Path(__file__).parent / "fixtures" / "openai_v0"


@pytest.fixture
def fixture_findings():
    """Findings for one fixture file, scanned on its own."""

    def _scan(name: str) -> list[Finding]:
        result = scan(FIXTURES / name, "openai-v1")
        assert result.parse_errors == []
        return result.findings

    return _scan


@pytest.fixture
def scan_code(tmp_path):
    """Write a snippet to a temp file and return its findings."""

    def _scan(code: str) -> list[Finding]:
        (tmp_path / "snippet.py").write_text(textwrap.dedent(code))
        return scan(tmp_path / "snippet.py", "openai-v1").findings

    return _scan


def summary(findings: list[Finding]) -> list[tuple[int, str]]:
    return [(f.line, f.category) for f in findings]


def at(findings: list[Finding], line: int, category: str | None = None) -> Finding:
    matches = [f for f in findings if f.line == line and category in (None, f.category)]
    assert len(matches) == 1, f"expected one finding at line {line}, got {matches}"
    return matches[0]
