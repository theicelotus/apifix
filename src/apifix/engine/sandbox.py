"""Run a repo's tests in Docker, in two phases.

1. Install (network on): only a generated requirements file is visible; the repo's own code is
   not, so nothing from the repo runs while the network is up.
2. Test (``--network none``): the repo copy is installed offline (editable, falling back to
   PYTHONPATH) and its tests run against the read-only dependencies from phase 1.

Containers run as a non-root user with CPU/memory/pid limits, a read-only root filesystem, no
capabilities and only a fixed set of environment variables. Containers and temp dirs are
always removed, including on timeouts and errors.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apifix.engine.docker import DockerCLI, DockerClient
from apifix.engine.project import (
    DEFAULT_PYTHON,
    InstallPlan,
    TestPlan,
    build_install_plan,
    detect_python_version,
    detect_tests,
)
from apifix.engine.test_results import (
    ReportError,
    TestCaseResult,
    TestCounts,
    parse_junit,
    parse_unittest_output,
)
from apifix.engine.walk import is_skipped_dir

Status = Literal["passed", "failed", "unverified", "error", "timeout"]
PhaseStatus = Literal["ok", "failed", "timeout", "error", "skipped"]
InstallMethod = Literal["editable", "pythonpath"]

DRIVER = Path(__file__).with_name("sandbox_driver.py")
NOBODY = 65534
DOCKER_RUN_FAILED = 125  # `docker run` itself failed (bad image, daemon error, ...)
KILLED = 137  # SIGKILL, typically the OOM killer

# The complete container environment. Literal values only: nothing is read from os.environ.
CONTAINER_ENV: dict[str, str] = {
    "HOME": "/tmp",
    "LANG": "C.UTF-8",
    "PIP_NO_INPUT": "1",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_NO_CACHE_DIR": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}

BUILD_ERROR_MARKERS = (
    "Failed building wheel",
    "Failed to build",
    "Could not build wheels",
    "subprocess-exited-with-error",
    "metadata-generation-failed",
    "legacy-install-failure",
    "fatal error:",
    "gcc",
    "pg_config",
    "command 'cc' failed",
)
RESOLUTION_ERROR_MARKERS = (
    "ResolutionImpossible",
    "conflicting dependencies",
    "No matching distribution found",
)


@dataclass(frozen=True)
class SandboxConfig:
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 512
    tmpfs_size: str = "512m"
    install_timeout_s: float = 900
    test_timeout_s: float = 600
    output_head_bytes: int = 4_000
    output_tail_bytes: int = 16_000
    default_python: str = DEFAULT_PYTHON
    image_repo: str = "python"
    max_install_attempts: int = 3


@dataclass
class PhaseResult:
    name: Literal["install", "test"]
    status: PhaseStatus
    image: str
    network: str
    command: list[str]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    constraints_applied: bool | None = None  # install phase only


@dataclass
class SandboxResult:
    status: Status
    reason: str
    python_version: str
    python_version_source: str
    runner: str | None = None
    test_command: list[str] = field(default_factory=list)
    image: str | None = None
    install_method: InstallMethod | None = None
    dependency_sources: list[str] = field(default_factory=list)
    lockfile: str | None = None
    lockfile_applied: bool = False
    notes: list[str] = field(default_factory=list)
    install_attempts: list[PhaseResult] = field(default_factory=list)
    test: PhaseResult | None = None
    counts: TestCounts = field(default_factory=TestCounts)
    tests: list[TestCaseResult] = field(default_factory=list)
    duration_s: float = 0.0


@dataclass(frozen=True)
class Mount:
    host: Path
    container: str
    writable: bool

    def arg(self) -> str:
        # `z` relabels for SELinux hosts (a no-op elsewhere); only ever applied to our temp dirs.
        return f"{self.host}:{self.container}:{'rw' if self.writable else 'ro'},z"


def container_user() -> str:
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        return f"{NOBODY}:{NOBODY}"
    return f"{uid}:{gid}"


def container_args(
    name: str,
    *,
    image: str,
    command: Sequence[str],
    network: Literal["bridge", "none"],
    mounts: Sequence[Mount],
    config: SandboxConfig,
    user: str,
    workdir: str | None = None,
) -> list[str]:
    """Arguments for `docker run`. Pure, so the security properties are unit-testable."""
    args = [
        "--name", name,
        "--label", "apifix=1",
        "--network", network,
        "--user", user,
        "--read-only",
        "--tmpfs", f"/tmp:rw,exec,nosuid,size={config.tmpfs_size}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--cpus", str(config.cpus),
        "--memory", config.memory,
        "--memory-swap", config.memory,
        "--pids-limit", str(config.pids_limit),
    ]  # fmt: skip
    for key, value in CONTAINER_ENV.items():
        args += ["--env", f"{key}={value}"]
    for m in mounts:
        args += ["--volume", m.arg()]
    if workdir:
        args += ["--workdir", workdir]
    return [*args, image, *command]


class _Workspace:
    """Host temp dirs bind-mounted into the containers; removed by cleanup()."""

    def __init__(self, repo: Path, plan: InstallPlan) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="apifix-sandbox-"))
        self.manifest = self.root / "manifest"
        self.deps = self.root / "deps"
        self.work = self.root / "work"
        self.results = self.root / "results"
        self.tooling = self.root / "tooling"
        for d in (self.manifest, self.deps, self.results, self.tooling):
            d.mkdir()
        (self.manifest / "requirements.txt").write_text("\n".join(plan.requirements) + "\n")
        (self.manifest / "constraints.txt").write_text("\n".join(plan.constraints) + "\n")
        shutil.copy2(DRIVER, self.tooling / "driver.py")
        shutil.copytree(repo, self.work, symlinks=True, ignore=_ignore_tooling_dirs)
        if os.getuid() == 0:  # containers run as nobody: let it write where it must
            for d in (self.deps, self.work, self.results):
                _make_world_writable(d)

    def reset_deps(self) -> None:
        shutil.rmtree(self.deps, ignore_errors=True)
        self.deps.mkdir()
        if os.getuid() == 0:
            _make_world_writable(self.deps)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def _ignore_tooling_dirs(directory: str, names: list[str]) -> set[str]:
    return {
        n for n in names if (Path(directory) / n).is_dir() and is_skipped_dir(Path(directory) / n)
    }


def _make_world_writable(path: Path) -> None:
    for p in [path, *path.rglob("*")]:
        if not p.is_symlink():
            p.chmod(p.stat().st_mode | 0o777 if p.is_dir() else p.stat().st_mode | 0o666)


def _has_marker(phase: PhaseResult, markers: Sequence[str]) -> bool:
    text = phase.stdout + phase.stderr
    return any(m in text for m in markers)


class Sandbox:
    def __init__(self, config: SandboxConfig | None = None, docker: DockerClient | None = None):
        self.config = config or SandboxConfig()
        self.docker = docker or DockerCLI(
            head_bytes=self.config.output_head_bytes, tail_bytes=self.config.output_tail_bytes
        )
        self.user = container_user()

    def run(self, repo_path: str | os.PathLike[str]) -> SandboxResult:
        start = time.monotonic()
        repo = Path(repo_path).resolve()
        if not repo.is_dir():
            raise NotADirectoryError(f"not a directory: {repo}")
        python = detect_python_version(repo, self.config.default_python)
        tests = detect_tests(repo)
        result = SandboxResult(
            status="error",
            reason="",
            python_version=python.version,
            python_version_source=python.source,
            runner=tests.runner,
            test_command=list(tests.command),
        )
        try:
            self._run(repo, tests, result)
        finally:
            result.duration_s = time.monotonic() - start
        return result

    # phases

    def _run(self, repo: Path, tests: TestPlan, result: SandboxResult) -> None:
        if not tests.test_files:
            result.status, result.reason = "unverified", "no test files found"
            return
        plan = build_install_plan(repo, tests.runner)
        result.dependency_sources = list(plan.sources)
        result.lockfile = plan.lockfile
        result.notes = list(plan.notes)

        usable, why = self.docker.available()
        if not usable:
            result.reason = why
            return
        workspace = _Workspace(repo, plan)
        try:
            image = self._install(workspace, plan, result)
            if image is None:
                return
            self._test(workspace, image, tests, result)
        finally:
            workspace.cleanup()

    def _install(self, ws: _Workspace, plan: InstallPlan, result: SandboxResult) -> str | None:
        """Install dependencies; return the image that worked, or None (result is filled in)."""
        image = f"{self.config.image_repo}:{result.python_version}-slim"
        use_constraints = bool(plan.constraints)
        for _ in range(self.config.max_install_attempts):
            ws.reset_deps()
            command = ["python", "/apifix/driver.py", "install"]
            if use_constraints:
                command.append("--constraints")
            mounts = [
                Mount(ws.manifest, "/manifest", writable=False),
                Mount(ws.deps, "/deps", writable=True),
                Mount(ws.tooling, "/apifix", writable=False),
            ]
            phase = self._phase("install", image, "bridge", command, mounts, None)
            phase.constraints_applied = use_constraints
            result.install_attempts.append(phase)
            result.image = image

            if phase.status == "ok":
                result.lockfile_applied = use_constraints
                return image
            if phase.status == "timeout":
                result.status = "timeout"
                result.reason = f"dependency install exceeded {self.config.install_timeout_s}s"
                return None
            if phase.status == "error":
                break
            if image.endswith("-slim") and _has_marker(phase, BUILD_ERROR_MARKERS):
                image = image.removesuffix("-slim")
                result.notes.append(f"build error on the slim image; retrying with {image}")
                continue
            if use_constraints and _has_marker(phase, RESOLUTION_ERROR_MARKERS):
                use_constraints = False
                result.notes.append(
                    f"{plan.lockfile} pins could not be resolved; retrying without them"
                )
                continue
            break
        result.status = "error"
        result.reason = "dependency install failed"
        return None

    def _test(self, ws: _Workspace, image: str, tests: TestPlan, result: SandboxResult) -> None:
        command = ["python", "/apifix/driver.py", "test", "--", *tests.command]
        mounts = [
            Mount(ws.deps, "/deps", writable=False),
            Mount(ws.work, "/work", writable=True),
            Mount(ws.results, "/results", writable=True),
            Mount(ws.tooling, "/apifix", writable=False),
        ]
        phase = self._phase("test", image, "none", command, mounts, "/work")
        result.test = phase

        driver = _read_json(ws.results / "driver.json")
        if driver.get("install_method") in ("editable", "pythonpath"):
            result.install_method = driver["install_method"]
            if driver["install_method"] == "pythonpath":
                result.notes.append("offline editable install failed; used PYTHONPATH instead")

        if phase.status == "timeout":
            result.status = "timeout"
            result.reason = f"tests exceeded {self.config.test_timeout_s}s"
            return
        if phase.exit_code == KILLED:
            result.status = "error"
            result.reason = (
                f"test container was killed (likely out of memory, limit {self.config.memory})"
            )
            return
        if phase.status == "error" or not driver:
            result.status = "error"
            result.reason = "the test phase did not start"
            return

        if tests.runner == "pytest":
            report = ws.results / "junit.xml"
            if not report.is_file():
                result.status = "unverified" if phase.exit_code == 5 else "error"
                result.reason = (
                    "pytest collected no tests"
                    if phase.exit_code == 5
                    else f"pytest exited {phase.exit_code} without a report"
                )
                return
            try:
                result.counts, result.tests = parse_junit(report.read_text(errors="replace"))
            except ReportError as exc:
                result.status, result.reason = "error", str(exc)
                return
        else:
            result.counts, result.tests = parse_unittest_output(phase.stderr + phase.stdout)

        c = result.counts
        if c.failed or c.errors:
            result.status = "failed"
            result.reason = f"{c.failed} failed, {c.errors} errors"
        elif c.passed == 0:
            result.status = "unverified"
            result.reason = "no tests ran" if c.total == 0 else f"all {c.total} tests skipped"
        elif phase.exit_code == 0:
            result.status = "passed"
            result.reason = f"{c.passed} passed, {c.skipped} skipped"
        else:
            result.status = "failed"
            result.reason = f"test runner exited {phase.exit_code}"

    def _phase(
        self,
        name: Literal["install", "test"],
        image: str,
        network: Literal["bridge", "none"],
        command: list[str],
        mounts: list[Mount],
        workdir: str | None,
    ) -> PhaseResult:
        container = f"apifix-{name}-{uuid.uuid4().hex[:12]}"
        args = container_args(
            container,
            image=image,
            command=command,
            network=network,
            mounts=mounts,
            config=self.config,
            user=self.user,
            workdir=workdir,
        )
        timeout = self.config.install_timeout_s if name == "install" else self.config.test_timeout_s
        try:
            proc = self.docker.run(args, timeout)
        finally:
            self.docker.remove_container(container)
        if proc.timed_out:
            status: PhaseStatus = "timeout"
        elif proc.exit_code == 0:
            status = "ok"
        elif proc.exit_code == DOCKER_RUN_FAILED:
            status = "error"
        else:
            status = "failed"
        return PhaseResult(
            name=name,
            status=status,
            image=image,
            network=network,
            command=command,
            exit_code=proc.exit_code,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=proc.duration_s,
        )


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_sandbox(
    repo_path: str | os.PathLike[str],
    config: SandboxConfig | None = None,
    docker: DockerClient | None = None,
) -> SandboxResult:
    return Sandbox(config, docker).run(repo_path)
