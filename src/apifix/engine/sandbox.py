"""Run a repo's tests in Docker, in two phases.

1. Install (network on): only a generated requirements file is visible; the repo's own code is
   not, so nothing from the repo runs while the network is up.
2. Test (``--network none``): the repo copy is installed offline (editable, falling back to
   PYTHONPATH) and its tests run against the read-only dependencies from phase 1.

Containers run as a non-root user with CPU/memory/pid limits, a read-only root filesystem, no
capabilities and only a fixed set of environment variables. Containers and temp dirs are
always removed, including on timeouts and errors. If the process dies anyway (crash, power
loss), `apifix cleanup` removes what it left behind: each workspace holds an flock for its
lifetime and each container is labeled with its workspace, so leftovers are recognisable.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import fcntl
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
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
    FileCoverage,
    ReportError,
    TestCaseResult,
    TestCounts,
    parse_coverage_json,
    parse_junit,
    parse_unittest_output,
)
from apifix.engine.walk import copy_repo

Status = Literal["passed", "failed", "unverified", "error", "timeout"]
PhaseStatus = Literal["ok", "failed", "timeout", "error", "skipped"]
InstallMethod = Literal["editable", "pythonpath"]

DRIVER = Path(__file__).with_name("sandbox_driver.py")
CONTAINER_LABEL = "apifix=1"
WORKSPACE_LABEL = "apifix.workspace"  # container label: the host workspace it mounts
WORKSPACE_PREFIX = "apifix-sandbox-"
WORKSPACE_LOCK = ".apifix.lock"  # flock held by the owning process while the workspace lives
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
# Added in the test phase only. .git is not copied into the sandbox, so setuptools-scm cannot
# derive a version and the offline editable install would fail without this.
TEST_PHASE_ENV: dict[str, str] = {
    "SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0",
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
    # Line coverage of the repo copy (paths relative to the repo), when requested and measured.
    coverage: dict[str, FileCoverage] | None = None
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
    extra_env: Mapping[str, str] | None = None,
    workspace: Path | None = None,
) -> list[str]:
    """Arguments for `docker run`. Pure, so the security properties are unit-testable."""
    args = [
        "--name", name,
        "--label", CONTAINER_LABEL,
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
    if workspace is not None:
        args += ["--label", f"{WORKSPACE_LABEL}={workspace}"]
    for key, value in {**CONTAINER_ENV, **(extra_env or {})}.items():
        args += ["--env", f"{key}={value}"]
    for m in mounts:
        args += ["--volume", m.arg()]
    if workdir:
        args += ["--workdir", workdir]
    return [*args, image, *command]


class _Workspace:
    """Host temp dirs bind-mounted into the containers; removed by cleanup()."""

    def __init__(self, repo: Path, plan: InstallPlan) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX))
        self._lock = lock_workspace(self.root)
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
        copy_repo(repo, self.work)
        if os.getuid() == 0:  # containers run as nobody: let it write where it must
            for d in (self.deps, self.work, self.results):
                _make_world_writable(d)

    def reset_work(self, repo: Path) -> None:
        """Fresh repo copy and empty results dir, so one test run cannot affect the next."""
        for d in (self.work, self.results):
            shutil.rmtree(d, ignore_errors=True)
        self.results.mkdir()
        copy_repo(repo, self.work)
        if os.getuid() == 0:
            for d in (self.work, self.results):
                _make_world_writable(d)

    def reset_deps(self) -> None:
        shutil.rmtree(self.deps, ignore_errors=True)
        self.deps.mkdir()
        if os.getuid() == 0:
            _make_world_writable(self.deps)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        if self._lock is not None:
            unlock_workspace(self._lock)
            self._lock = None


def lock_workspace(root: Path) -> int | None:
    """Take the workspace's lock without blocking; return its fd, or None if it is held. The
    kernel releases the lock when the holder exits, however it exits."""
    fd = os.open(root / WORKSPACE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def unlock_workspace(fd: int) -> None:
    with contextlib.suppress(OSError):
        os.close(fd)


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

    def session(
        self, repo_path: str | os.PathLike[str], *, coverage: bool = False
    ) -> SandboxSession:
        """Install once, run the test phase as often as needed. Use as a context manager."""
        return SandboxSession(self, repo_path, coverage=coverage)

    def run(self, repo_path: str | os.PathLike[str], *, coverage: bool = False) -> SandboxResult:
        with self.session(repo_path, coverage=coverage) as session:
            return session.run_tests()

    # phases

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
            phase = self._phase("install", image, "bridge", command, mounts, None, ws.root)
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

    def _test(
        self,
        ws: _Workspace,
        image: str,
        tests: TestPlan,
        result: SandboxResult,
        coverage: bool,
    ) -> None:
        command = ["python", "/apifix/driver.py", "test"]
        command += ["--coverage", "--", *tests.command] if coverage else ["--", *tests.command]
        mounts = [
            Mount(ws.deps, "/deps", writable=False),
            Mount(ws.work, "/work", writable=True),
            Mount(ws.results, "/results", writable=True),
            Mount(ws.tooling, "/apifix", writable=False),
        ]
        phase = self._phase("test", image, "none", command, mounts, "/work", ws.root)
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
        if coverage:
            result.coverage = self._read_coverage(ws, result)

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

    @staticmethod
    def _read_coverage(ws: _Workspace, result: SandboxResult) -> dict[str, FileCoverage] | None:
        report = ws.results / "coverage.json"
        if not report.is_file():
            result.notes.append("coverage was requested but no report was produced")
            return None
        try:
            return parse_coverage_json(report.read_text(errors="replace"))
        except ReportError as exc:
            result.notes.append(f"coverage report unusable: {exc}")
            return None

    def _phase(
        self,
        name: Literal["install", "test"],
        image: str,
        network: Literal["bridge", "none"],
        command: list[str],
        mounts: list[Mount],
        workdir: str | None,
        workspace: Path,
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
            extra_env=TEST_PHASE_ENV if name == "test" else None,
            workspace=workspace,
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


class SandboxSession:
    """One repo, one dependency install, any number of test runs.

    Each `run_tests()` returns a full SandboxResult (install details included) and starts from a
    fresh copy of the repo. `close()` removes everything; prefer `with sandbox.session(...)`.
    """

    def __init__(
        self, sandbox: Sandbox, repo_path: str | os.PathLike[str], *, coverage: bool
    ) -> None:
        self.sandbox = sandbox
        self.repo = Path(repo_path).resolve()
        if not self.repo.is_dir():
            raise NotADirectoryError(f"not a directory: {self.repo}")
        self.coverage = coverage
        python = detect_python_version(self.repo, sandbox.config.default_python)
        self.tests = detect_tests(self.repo)
        self.base = SandboxResult(
            status="error",
            reason="",
            python_version=python.version,
            python_version_source=python.source,
            runner=self.tests.runner,
            test_command=list(self.tests.command),
        )
        self._workspace: _Workspace | None = None
        self._image: str | None = None
        self._prepared = False
        self._runs = 0

    def __enter__(self) -> SandboxSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None

    def _prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        base = self.base
        if not self.tests.test_files:
            base.status, base.reason = "unverified", "no test files found"
            return
        plan = build_install_plan(self.repo, self.tests.runner)
        if self.coverage and "coverage" not in plan.requirements:
            plan = dataclasses.replace(plan, requirements=(*plan.requirements, "coverage"))
        base.dependency_sources = list(plan.sources)
        base.lockfile = plan.lockfile
        base.notes = list(plan.notes)

        usable, why = self.sandbox.docker.available()
        if not usable:
            base.reason = why
            return
        self._workspace = _Workspace(self.repo, plan)
        self._image = self.sandbox._install(self._workspace, plan, base)

    def run_tests(self, *, coverage: bool | None = None) -> SandboxResult:
        """Run the test phase. `coverage` defaults to the session setting (it can only be
        turned on if the session was opened with coverage=True, since it must be installed)."""
        self._prepare()
        result = copy.deepcopy(self.base)
        try:
            if self._image is None or self._workspace is None:
                return result
            if self._runs:
                self._workspace.reset_work(self.repo)
            self._runs += 1
            measure = self.coverage if coverage is None else (coverage and self.coverage)
            self.sandbox._test(self._workspace, self._image, self.tests, result, measure)
            return result
        finally:
            # This run's cost: the (shared) install plus its own test phase.
            phases = [*result.install_attempts, *([result.test] if result.test else [])]
            result.duration_s = sum(p.duration_s for p in phases)
