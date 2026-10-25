"""Which apifix produced a run: package version plus the git commit it was running from."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from apifix import __version__

_SOURCE_DIR = Path(__file__).resolve().parent


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(_SOURCE_DIR), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def apifix_build() -> dict[str, Any]:
    """`{"version", "commit", "dirty"}`; commit and dirty are None outside a git checkout."""
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain") if commit else None
    return {
        "version": __version__,
        "commit": commit,
        "dirty": None if status is None else bool(status),
    }


def build_label(build: dict[str, Any] | None = None) -> str:
    """`0.1.0 (commit 3edbf3b, dirty)`-style text; `-dirty` is appended to the commit."""
    build = build or apifix_build()
    commit = build["commit"]
    if not commit:
        return f"{build['version']} (git commit unknown)"
    return f"{build['version']} (git commit {commit[:12]}{'-dirty' if build['dirty'] else ''})"
