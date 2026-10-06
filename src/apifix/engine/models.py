"""Data types shared by the scanner and migrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Category = Literal[
    "import",
    "client_config",
    "api_call",
    "error_class",
    "response_access",
    "removed_module",
    "dependency",
]
Confidence = Literal["certain", "likely"]

CERTAIN: Confidence = "certain"
LIKELY: Confidence = "likely"


@dataclass(frozen=True, order=True)
class Finding:
    """One place in a repo that must change.

    ``file`` is relative to the scan root (POSIX separators). ``line`` and ``col`` are 1-based,
    so ``file:line:col`` can be pasted into an editor.
    """

    file: str
    line: int
    col: int
    end_line: int
    end_col: int
    category: Category
    old_pattern: str
    suggestion: str
    confidence: Confidence = CERTAIN


@dataclass(frozen=True)
class ParseError:
    file: str
    error: str


@dataclass
class ScanResult:
    migration: str
    root: str
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    parse_errors: list[ParseError] = field(default_factory=list)
