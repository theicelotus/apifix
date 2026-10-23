"""Data types shared by the scanner and migrations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from apifix.engine.already_migrated import AlreadyMigrated

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


@dataclass(frozen=True)
class SmokeCheck:
    """A migration's post-install check: `code` runs in the sandbox with the installed
    dependencies (no network, no repo code) and must exit 0. `distribution` is the migrated
    library, whose installed version and requirements are reported when it fails."""

    distribution: str
    code: str
    description: str


@dataclass(frozen=True)
class CompanionPin:
    """What the migrated library needs from one of its own requirements: versions
    `>= minimum` and `< upper`. A repo pin that allows none of them holds the library back, and
    apifix raises it to that range; with `keep_major`, to the part of it within the pin's
    current major version (so code written for that major keeps working)."""

    minimum: str
    upper: str
    keep_major: bool = False


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
    # set when the findings are all stale tests of code already on the new API
    already_migrated: AlreadyMigrated | None = None
