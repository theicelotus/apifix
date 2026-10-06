"""Remove what an interrupted apifix run left behind: containers and temp workspaces.

Normally the sandbox removes both itself. After a crash, SIGKILL or power loss it cannot, and
the leftovers waste disk (the deps venv) and keep containers around. A workspace is stale when
no process holds its flock (the kernel drops the lock when its holder dies); a container is
orphaned when its labeled workspace is stale or gone. Workspaces of live runs, and anything
owned by another user, are never touched.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apifix.engine.docker import DockerClient, DockerError
from apifix.engine.sandbox import (
    CONTAINER_LABEL,
    WORKSPACE_LOCK,
    WORKSPACE_PREFIX,
    lock_workspace,
    unlock_workspace,
)

# A workspace this young may be between mkdtemp() and taking its lock: leave it alone.
MIN_AGE_S = 60.0

WorkspaceState = Literal["stale", "in_use", "not_ours", "missing"]


@dataclass
class CleanupReport:
    dry_run: bool
    removed_containers: list[str] = field(default_factory=list)  # "name (state)"
    removed_dirs: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "what: why"
    errors: list[str] = field(default_factory=list)


class _Locks:
    """Locks taken on stale workspaces, held until cleanup ends so no run can adopt them."""

    def __init__(self, min_age_s: float) -> None:
        self.min_age_s = min_age_s
        self.fds: dict[Path, int] = {}
        self.states: dict[Path, WorkspaceState] = {}

    def state(self, root: Path) -> WorkspaceState:
        if root not in self.states:
            self.states[root] = self._check(root)
        return self.states[root]

    def _check(self, root: Path) -> WorkspaceState:
        if not root.name.startswith(WORKSPACE_PREFIX) or not root.is_absolute():
            return "not_ours"
        try:
            st = root.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "not_ours"
        if root.is_symlink() or not root.is_dir() or st.st_uid != os.getuid():
            return "not_ours"
        if time.time() - st.st_mtime < self.min_age_s:
            return "in_use"
        if not (root / WORKSPACE_LOCK).exists():
            return "stale"  # created by a version without locks, or died before locking
        try:
            fd = lock_workspace(root)
        except OSError:
            return "not_ours"
        if fd is None:
            return "in_use"
        self.fds[root] = fd
        return "stale"

    def release(self) -> None:
        for fd in self.fds.values():
            unlock_workspace(fd)
        self.fds.clear()


def cleanup(
    docker: DockerClient | None,
    *,
    temp_dir: Path | None = None,
    dry_run: bool = False,
    min_age_s: float = MIN_AGE_S,
) -> CleanupReport:
    """Remove orphaned apifix containers, then stale workspaces under `temp_dir` (default: the
    system temp dir) and wherever orphaned containers say their workspace was."""
    report = CleanupReport(dry_run=dry_run)
    locks = _Locks(min_age_s)
    roots: set[Path] = set()
    keep: set[Path] = set()  # workspaces still mounted by a container we could not remove
    try:
        if docker is not None:
            roots |= _containers(docker, locks, report, keep)
        base = Path(temp_dir) if temp_dir is not None else Path(tempfile.gettempdir())
        try:
            roots |= {p for p in base.iterdir() if p.name.startswith(WORKSPACE_PREFIX)}
        except OSError as exc:
            report.errors.append(f"cannot list {base}: {exc}")
        for root in sorted(roots - keep):
            _workspace(root, locks, report)
    finally:
        locks.release()
    return report


def _containers(
    docker: DockerClient, locks: _Locks, report: CleanupReport, keep: set[Path]
) -> set[Path]:
    usable, why = docker.available()
    if not usable:
        report.errors.append(f"containers not checked: {why}")
        return set()
    try:
        containers = docker.list_containers(CONTAINER_LABEL)
    except DockerError as exc:
        report.errors.append(f"containers not checked: {exc}")
        return set()

    roots: set[Path] = set()
    removed = []
    for c in containers:
        label = f"container {c.name} ({c.state})"
        if c.workspace:
            root = Path(c.workspace)
            state = locks.state(root)
            if state == "in_use":
                report.skipped.append(f"{label}: its run is still in progress")
                continue
            if state == "not_ours":
                report.skipped.append(f"{label}: workspace {root} is not this user's")
                continue
            roots.add(root)
        elif c.state == "running":
            # From an apifix version that did not label workspaces: cannot tell if it is live.
            report.skipped.append(
                f"{label}: running and has no workspace label; "
                f"remove it with `docker rm -f {c.name}` if no apifix run is active"
            )
            continue
        if not report.dry_run:
            docker.remove_container(c.id)
        removed.append(c)

    if removed and not report.dry_run:
        try:
            still_there = {c.id for c in docker.list_containers(CONTAINER_LABEL)}
        except DockerError as exc:
            report.errors.append(f"could not confirm container removal: {exc}")
            keep.update(Path(c.workspace) for c in removed if c.workspace)
            return roots
        for c in removed:
            if c.id in still_there:
                report.errors.append(f"container {c.name} could not be removed")
                if c.workspace:
                    keep.add(Path(c.workspace))  # its mounts are still in use
            else:
                report.removed_containers.append(f"{c.name} ({c.state})")
    else:
        report.removed_containers += [f"{c.name} ({c.state})" for c in removed]
    return roots


def _workspace(root: Path, locks: _Locks, report: CleanupReport) -> None:
    state = locks.state(root)
    if state == "missing":
        return
    if state == "in_use":
        report.skipped.append(f"{root}: in use by a running apifix process (or just created)")
        return
    if state == "not_ours":
        report.skipped.append(f"{root}: not owned by this user")
        return
    if not report.dry_run:
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            report.errors.append(f"{root} could not be fully removed")
            return
    report.removed_dirs.append(str(root))
