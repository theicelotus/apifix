"""Walk a repo's files the same way everywhere: skip tooling dirs, never follow symlinks."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    ".eggs",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "site-packages",
    "build",
    "dist",
}


def is_skipped_dir(path: Path) -> bool:
    return (
        path.name in SKIP_DIRS
        or path.name.endswith(".egg-info")
        or path.is_symlink()
        or (path / "pyvenv.cfg").exists()  # a virtualenv under another name
    )


def iter_files(root: Path) -> Iterator[Path]:
    """Yield regular files under ``root`` (or ``root`` itself), sorted, without symlinks."""
    if root.is_file():
        if not root.is_symlink():
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not is_skipped_dir(here / d))
        for name in sorted(filenames):
            file = here / name
            if not file.is_symlink():
                yield file


def copy_repo(src: Path, dst: Path) -> None:
    """Copy a repo without its tooling dirs (.git, venvs, caches). Symlinks are copied as links,
    never followed."""
    shutil.copytree(src, dst, symlinks=True, ignore=_ignore_skipped_dirs)


def _ignore_skipped_dirs(directory: str, names: list[str]) -> set[str]:
    here = Path(directory)
    return {n for n in names if (here / n).is_dir() and is_skipped_dir(here / n)}
