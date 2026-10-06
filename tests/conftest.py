from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apifix.engine.models import Finding
from apifix.engine.scanner import scan

FIXTURES = Path(__file__).parent / "fixtures" / "openai_v0"
# Fixture repos are data (some contain their own tests); never collect them.
collect_ignore = ["fixtures"]


# --- Docker and LLM tests are opt-in ----------------------------------------------------------

OPT_IN = {
    "docker": "also run tests marked `docker` (real Docker daemon + network; slow)",
    "llm": "also run tests marked `llm` (real LLM API calls; cost money, need an API key)",
    "local_llm": "also run tests marked `local_llm` (a local Ollama; slow, needs a model)",
}


def pytest_addoption(parser):
    for marker, help in OPT_IN.items():
        parser.addoption(f"--{marker.replace('_', '-')}", action="store_true", help=help)


def pytest_collection_modifyitems(config, items):
    markexpr = config.getoption("markexpr") or ""
    words = set(markexpr.replace("(", " ").replace(")", " ").split())
    negated = {w for w in words if w != "not"} & {
        m for m in OPT_IN if f"not {m}" in " ".join(markexpr.split())
    }
    keep = {
        m
        for m in OPT_IN
        if config.getoption(f"--{m.replace('_', '-')}") or (m in words and m not in negated)
    }
    deselected = [
        item for item in items if any(item.get_closest_marker(m) for m in set(OPT_IN) - keep)
    ]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if item not in deselected]


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
