"""Run a repo's tests in Docker, in two phases.

1. Install (network on): only a generated requirements file is visible; the repo's own code is
   not, so nothing from the repo runs while the network is up. An optional persistent pip
   cache (`SandboxConfig.pip_cache_dir`) is mounted here and nowhere else, so packages
   downloaded once are reused by later runs.
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
import re
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apifix.engine.dependencies import normalize_name
from apifix.engine.docker import DockerCLI, DockerClient
from apifix.engine.models import LibraryTarget, SmokeCheck
from apifix.engine.project import (
    DEFAULT_PYTHON,
    DroppedSource,
    InstallPlan,
    NotInstalled,
    SandboxRequirements,
    TestPlan,
    build_install_plan,
    detect_python_version,
    detect_tests,
    exact_pins,
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
SMOKE_MARKER = "APIFIX_SMOKE_RESULT "  # sandbox_driver.SMOKE_MARKER
INSTALL_STATS_MARKER = "APIFIX_INSTALL_STATS "  # sandbox_driver.INSTALL_STATS_MARKER
PIP_CACHE_MOUNT = "/pipcache"
# Created in a pip cache dir that apifix made. Only such a dir is mounted (bind mounts are
# relabeled for SELinux), never an arbitrary existing directory.
PIP_CACHE_MARKER = ".apifix-pip-cache"
PIP_CACHE_ENV = "APIFIX_PIP_CACHE"
NO_PROJECT = "no pyproject.toml or setup.py"  # sandbox_driver.NO_PROJECT
CONTAINER_LABEL = "apifix=1"
WORKSPACE_LABEL = "apifix.workspace"  # container label: the host workspace it mounts
WORKSPACE_PREFIX = "apifix-sandbox-"
WORKSPACE_LOCK = ".apifix.lock"  # flock held by the owning process while the workspace lives
NOBODY = 65534
DOCKER_RUN_FAILED = 125  # `docker run` itself failed (bad image, daemon error, ...)
KILLED = 137  # SIGKILL, typically the OOM killer
# pytest's exit code for a usage error: bad options, a conftest that fails to import, a missing
# plugin. It stops before collecting, so no JUnit report is written.
PYTEST_USAGE_ERROR = 4
RUNNER_ERROR_CHARS = 500  # SandboxResult.runner_error
RUNNER_OUTPUT_CHARS = 2_000  # SandboxResult.runner_output (trajectory only)

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
# pip output that means the network or the index failed, not the requirements: the install is
# retried once. A failure with no recognised marker at all is retried once too.
NETWORK_ERROR_MARKERS = (
    "NewConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "Read timed out",
    "ConnectionResetError",
    "Connection reset by peer",
    "RemoteDisconnected",
    "IncompleteRead",
    "ProtocolError",
    "Max retries exceeded",
    "Temporary failure in name resolution",
    "Name or service not known",
    "Network is unreachable",
    "SSLError",
    "HTTP error 429",
    "HTTP error 5",
    # pip found no release at all, which is what an unreachable index looks like too (also
    # printed for a misspelt name, which then costs one retry)
    "(from versions: none)",
)
# A network-looking install failure is retried up to this many times (so at most this many
# attempts beyond the first), independently of the resolution/build-error retries below.
MAX_NETWORK_INSTALL_RETRIES = 3
NETWORK_RETRY_NOTE = "dependency install failed (network or unknown cause)"
# pip output that means the requirements themselves are wrong: never retried as transient.
REQUIREMENT_ERROR_MARKERS = (
    "Invalid requirement",
    "InvalidRequirement",
    "Could not find a version that satisfies",
    "requires a different Python",
    "is not a supported wheel",
)
RESOLUTION_ERROR_MARKERS = (
    "ResolutionImpossible",
    "conflicting dependencies",
    "No matching distribution found",
)
# pip found the requirements themselves contradictory: never retried as transient, even when
# its output also has a network marker (an outage prints "No matching distribution", not these).
CONFLICT_MARKERS = ("ResolutionImpossible", "conflicting dependencies")
MAX_CONFLICT_LINES = 20
_MANIFEST_LINE_RE = re.compile(r"-([rc]) /manifest/(requirements|constraints)\.txt \(line (\d+)\)")
_REQUESTED_RE = re.compile(r"The user requested (?:\(constraint\) )?([A-Za-z0-9][A-Za-z0-9._-]*)")


def install_conflict(output: str, plan: InstallPlan) -> list[str]:
    """pip's conflict lines from a ResolutionImpossible failure: "Cannot install ..." and the
    list under "The conflict is caused by:", with the generated manifest's `-r
    /manifest/requirements.txt (line N)` shown as the requirement itself (and a constraint as
    the lockfile's pin). At most MAX_CONFLICT_LINES lines of 300 characters."""

    def manifest_line(m: re.Match[str]) -> str:
        lines = plan.requirements if m.group(1) == "r" else plan.constraints
        n = int(m.group(3))
        if not 1 <= n <= len(lines):
            return m.group(0)
        return lines[n - 1] if m.group(1) == "r" else f"{lines[n - 1]} (lockfile constraint)"

    out: list[str] = []
    in_causes = False
    for raw in output.splitlines():
        line = raw.rstrip()
        if line.strip() == "The conflict is caused by:":
            in_causes = True
            out.append("The conflict is caused by:")
            continue
        if in_causes:
            if line.startswith((" ", "\t")) and line.strip():
                out.append("    " + line.strip())
                continue
            in_causes = False
        if line.startswith("ERROR: Cannot install "):
            out.append(line.removeprefix("ERROR: "))
    out = [_MANIFEST_LINE_RE.sub(manifest_line, line) for line in dict.fromkeys(out)]
    return [line if len(line) <= 300 else line[:297] + "..." for line in out[:MAX_CONFLICT_LINES]]


def conflict_sources(
    lines: Sequence[str], declared: Mapping[str, Sequence[str]]
) -> dict[str, list[str]]:
    """Package -> the repo files that declare it, for each package pip's conflict lines say
    the user requested."""
    out: dict[str, list[str]] = {}
    for line in lines:
        for m in _REQUESTED_RE.finditer(line):
            name = normalize_name(m.group(1))
            if name in declared:
                out.setdefault(name, list(declared[name]))
    return out


@dataclass(frozen=True)
class SandboxConfig:
    cpus: float = 2.0
    memory: str = "2g"
    pids_limit: int = 512
    tmpfs_size: str = "512m"
    install_timeout_s: float = 900
    test_timeout_s: float = 600
    # Pulling the image, outside install_timeout_s (a slow or first pull must not eat the
    # install phase's own budget; see Sandbox._pull_image).
    image_pull_timeout_s: float = 300
    output_head_bytes: int = 4_000
    output_tail_bytes: int = 16_000
    default_python: str = DEFAULT_PYTHON
    image_repo: str = "python"
    # 1 initial attempt + up to MAX_NETWORK_INSTALL_RETRIES network retries, with room left for
    # an image switch or a lockfile-conflict retry in the same run.
    max_install_attempts: int = 6
    # Persistent pip cache shared by install phases (None: off). See default_pip_cache_dir().
    pip_cache_dir: Path | None = None
    # A user's requirements file that replaces dependency detection (--sandbox-requirements).
    requirements_override: SandboxRequirements | None = None


def default_pip_cache_dir() -> Path:
    """$APIFIX_PIP_CACHE, else $XDG_CACHE_HOME/apifix/pip, else ~/.cache/apifix/pip."""
    explicit = os.environ.get(PIP_CACHE_ENV)
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "apifix" / "pip"


def prepare_pip_cache(path: Path) -> tuple[Path | None, str]:
    """The cache dir to mount, created (with PIP_CACHE_MARKER) if needed; or None and why not.
    A non-empty dir without the marker was not made by apifix and is never mounted."""
    try:
        path = path.expanduser().resolve()
        if path.exists():
            if not path.is_dir():
                return None, f"pip cache {path} is not a directory; not used"
            if not (path / PIP_CACHE_MARKER).is_file() and any(path.iterdir()):
                return None, (
                    f"pip cache {path} was not created by apifix (no {PIP_CACHE_MARKER} in "
                    "it); not used"
                )
        path.mkdir(parents=True, exist_ok=True)
        (path / PIP_CACHE_MARKER).touch()
        if os.getuid() == 0:  # containers run as nobody: let it write the cache
            path.chmod(0o777)
    except OSError as exc:
        return None, f"pip cache {path} is not usable ({exc}); not used"
    return path, ""


@dataclass(frozen=True)
class DownloadStats:
    """What one install's pip fetched: distribution files downloaded vs taken from the cache."""

    downloaded: int = 0
    downloaded_bytes: int = 0
    cached: int = 0
    cached_bytes: int = 0


def parse_install_stats(stdout: str) -> DownloadStats | None:
    """The driver's INSTALL_STATS_MARKER line (the last one), or None if there is none."""
    line = next(
        (ln for ln in reversed(stdout.splitlines()) if ln.startswith(INSTALL_STATS_MARKER)), None
    )
    try:
        data = json.loads(line[len(INSTALL_STATS_MARKER) :]) if line else None
        return DownloadStats(**{k: int(data[k]) for k in DownloadStats.__dataclass_fields__})
    except (ValueError, TypeError, KeyError):
        return None


@dataclass
class PhaseResult:
    name: Literal["install", "test", "smoke"]
    status: PhaseStatus
    image: str
    network: str
    command: list[str]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    constraints_applied: bool | None = None  # install phase only
    downloads: DownloadStats | None = None  # install phase only, when the driver reported it


@dataclass
class SmokeResult:
    """Outcome of a migration's smoke check (see SmokeCheck) after the install."""

    status: Literal["ok", "failed", "error"]
    description: str
    output: str = ""  # the check's output (the traceback when it failed)
    distribution: str = ""
    version: str | None = None  # installed version of the migrated library
    requires: dict[str, str | None] = field(default_factory=dict)  # its requirements, installed
    pins: list[str] = field(default_factory=list)  # exact pins on any of those, from the repo
    # each of `pins` -> where it is declared (files, or the --sandbox-requirements label)
    pin_sources: dict[str, list[str]] = field(default_factory=dict)


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
    # Time spent pulling the install image(s), outside install_timeout_s. 0 if the image was
    # already present or no pull was needed.
    image_pull_s: float = 0.0
    test: PhaseResult | None = None
    counts: TestCounts = field(default_factory=TestCounts)
    tests: list[TestCaseResult] = field(default_factory=list)
    # Line coverage of the repo copy (paths relative to the repo), when requested and measured.
    coverage: dict[str, FileCoverage] | None = None
    duration_s: float = 0.0
    # The install failed in a way that looked transient and was run again (once).
    install_retried: bool = False
    # Test-phase environment variables set to placeholders (names only; see Sandbox.session).
    env_placeholders: list[str] = field(default_factory=list)
    # Repo dirs put on PYTHONPATH in the test phase (TestPlan.import_paths).
    import_paths: list[str] = field(default_factory=list)
    # nothing collected with those dirs, so the test phase was run again without them
    retried_without_import_paths: bool = False
    smoke: SmokeResult | None = None
    pip_cache: bool = False  # the install phase used the persistent pip cache
    # From the install plan: normalised name -> where it is declared; declared dependencies left
    # out on purpose; the --sandbox-requirements label and its lines the fix's declarations
    # replaced, when a user's file replaced detection.
    declared: dict[str, list[str]] = field(default_factory=dict)
    not_installed: list[NotInstalled] = field(default_factory=list)
    requirements_override: str | None = None
    override_replaced: list[str] = field(default_factory=list)
    # its lines for the migrated library that the target spec replaced (InstallPlan.upgraded)
    override_upgraded: list[str] = field(default_factory=list)
    # pytest stopped without a report: the error it printed (see pytest_error) and the tail of
    # its output
    runner_error: str | None = None
    runner_output: str = ""
    # dependency files left out because their pins conflict with an installed one's
    dropped_sources: list[DroppedSource] = field(default_factory=list)
    # pip's conflict lines when the install failed with ResolutionImpossible (install_conflict)
    install_conflict: list[str] = field(default_factory=list)


_ARGPARSE_ERROR_RE = re.compile(r"^\S+: error: .+")
_CONFTEST_RE = re.compile(r"^(\w+(?:Error|Exception|Failure)?) while loading conftest '([^']+)'")


def _without_driver_lines(output: str) -> str:
    return "\n".join(ln for ln in output.splitlines() if not ln.startswith("[apifix]"))


def pytest_error(output: str) -> str | None:
    """The error pytest printed when it stopped before writing a report, in one line: a
    conftest that failed to load ("ImportError while loading conftest 'tests/conftest.py':
    ValueError: ..."), an argparse usage error ("pytest: error: unrecognized arguments:
    --cov"), else its last `ERROR:` line, its last `E   ` line or the last line. Container
    paths under /work/ are shown relative to the repo. None for empty output."""
    lines = [ln.rstrip() for ln in output.replace("/work/", "").splitlines() if ln.strip()]
    if not lines:
        return None
    errors = [ln[1:].strip() for ln in lines if ln.startswith("E ")]
    found = None
    for i, line in enumerate(lines):
        if m := _CONFTEST_RE.match(line):
            last = next(
                (ln[1:].strip() for ln in reversed(lines[i + 1 :]) if ln.startswith("E ")), None
            )
            found = f"{m.group(1)} while loading conftest '{m.group(2)}'" + (
                f": {last}" if last else ""
            )
            break
    if found is None:
        found = next((ln.strip() for ln in lines if _ARGPARSE_ERROR_RE.match(ln.strip())), None)
    if found is None:
        found = next(
            (
                ln.strip()
                for ln in reversed(lines)
                if ln.startswith("ERROR: ") and not ln.startswith("ERROR: usage:")
            ),
            None,
        )
    if found is None:
        found = errors[-1] if errors else lines[-1].strip()
    if len(found) > RUNNER_ERROR_CHARS:
        found = found[: RUNNER_ERROR_CHARS - 3] + "..."
    return found


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
    env = {**CONTAINER_ENV, **(extra_env or {})}
    if "PIP_CACHE_DIR" in env:  # pip treats any value of PIP_NO_CACHE_DIR as "no cache"
        del env["PIP_NO_CACHE_DIR"]
    for key, value in env.items():
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
        self,
        repo_path: str | os.PathLike[str],
        *,
        coverage: bool = False,
        env: Mapping[str, str] | None = None,
        smoke: SmokeCheck | None = None,
        tests: TestPlan | None = None,
        original: str | os.PathLike[str] | None = None,
        target: LibraryTarget | None = None,
    ) -> SandboxSession:
        """Install once, run the test phase as often as needed. Use as a context manager.
        `env` adds test-phase variables: literal placeholders chosen by apifix (e.g. a fake
        API key the repo's CI also sets), never values from the host environment. `smoke`
        runs once after the install (SmokeResult in every result). `tests` replaces the
        detected test plan. `original`: the tree `repo_path` is a fix of, so that with a
        --sandbox-requirements file the fix's dependency edits still apply, and (`target`,
        the migrated library) the file's old pin of the library gets the target spec when the
        fix did not change the library."""
        return SandboxSession(
            self,
            repo_path,
            coverage=coverage,
            env=env,
            smoke=smoke,
            tests=tests,
            original=original,
            target=target,
        )

    def run(
        self,
        repo_path: str | os.PathLike[str],
        *,
        coverage: bool = False,
        env: Mapping[str, str] | None = None,
        smoke: SmokeCheck | None = None,
        tests: TestPlan | None = None,
        original: str | os.PathLike[str] | None = None,
        target: LibraryTarget | None = None,
    ) -> SandboxResult:
        with self.session(
            repo_path,
            coverage=coverage,
            env=env,
            smoke=smoke,
            tests=tests,
            original=original,
            target=target,
        ) as session:
            return session.run_tests()

    # phases

    def _pull_image(self, image: str, result: SandboxResult) -> None:
        """Pull `image` ahead of the install phase, outside install_timeout_s: pulling a large
        base image for the first time must not eat the install's own time budget. Not fatal if
        it fails or times out; the install phase falls back to docker run's implicit pull."""
        proc = self.docker.pull(image, self.config.image_pull_timeout_s)
        result.image_pull_s += proc.duration_s
        if proc.timed_out:
            result.notes.append(
                f"pulling {image} exceeded {self.config.image_pull_timeout_s:,.0f}s; "
                "continuing (the install phase will pull it as needed)"
            )
        elif proc.exit_code != 0:
            result.notes.append(
                f"docker pull {image} failed; continuing (the install phase will pull it as needed)"
            )

    def _install(self, ws: _Workspace, plan: InstallPlan, result: SandboxResult) -> str | None:
        """Install dependencies; return the image that worked, or None (result is filled in)."""
        image = f"{self.config.image_repo}:{result.python_version}-slim"
        use_constraints = bool(plan.constraints)
        known = (*BUILD_ERROR_MARKERS, *RESOLUTION_ERROR_MARKERS, *REQUIREMENT_ERROR_MARKERS)
        cache, env = None, None
        network_retries = 0
        if self.config.pip_cache_dir is not None:
            cache, why = prepare_pip_cache(self.config.pip_cache_dir)
            if cache is None:
                result.notes.append(why)
            else:
                env = {"PIP_CACHE_DIR": PIP_CACHE_MOUNT}
                result.pip_cache = True
        self._pull_image(image, result)
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
            if cache is not None:
                mounts.append(Mount(cache, PIP_CACHE_MOUNT, writable=True))
            phase = self._phase("install", image, "bridge", command, mounts, None, ws.root, env)
            phase.constraints_applied = use_constraints
            phase.downloads = parse_install_stats(phase.stdout)
            result.install_attempts.append(phase)
            result.image = image

            if phase.status == "ok":
                result.lockfile_applied = use_constraints
                result.install_conflict = []  # an earlier attempt's conflict was resolved
                return image
            if phase.status == "timeout":
                result.status = "timeout"
                result.reason = f"dependency install exceeded {self.config.install_timeout_s}s"
                return None
            if phase.status == "error":
                break
            conflict = _has_marker(phase, CONFLICT_MARKERS)
            if conflict:
                result.install_conflict = install_conflict(phase.stdout + "\n" + phase.stderr, plan)
            if (
                network_retries < MAX_NETWORK_INSTALL_RETRIES
                and not conflict
                and (_has_marker(phase, NETWORK_ERROR_MARKERS) or not _has_marker(phase, known))
            ):
                network_retries += 1
                result.install_retried = True
                result.notes[:] = [n for n in result.notes if not n.startswith(NETWORK_RETRY_NOTE)]
                result.notes.append(f"{NETWORK_RETRY_NOTE}; retried {network_retries} time(s)")
                continue
            if image.endswith("-slim") and _has_marker(phase, BUILD_ERROR_MARKERS):
                image = image.removesuffix("-slim")
                result.notes.append(f"build error on the slim image; retrying with {image}")
                self._pull_image(image, result)
                continue
            if use_constraints and _has_marker(phase, RESOLUTION_ERROR_MARKERS):
                causes = [line for line in result.install_conflict if line.startswith("    ")]
                if conflict and causes and not any("(constraint)" in c for c in causes):
                    result.notes.append(
                        f"pip's conflict involves no {plan.lockfile} pin; not retried without them"
                    )
                    break
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
        env: Mapping[str, str] | None = None,
    ) -> None:
        command = ["python", "/apifix/driver.py", "test"]
        if coverage:
            command.append("--coverage")
        for path in tests.import_paths:
            command += ["--path", path]
        command += ["--", *tests.command]
        mounts = [
            Mount(ws.deps, "/deps", writable=False),
            Mount(ws.work, "/work", writable=True),
            Mount(ws.results, "/results", writable=True),
            Mount(ws.tooling, "/apifix", writable=False),
        ]
        phase = self._phase("test", image, "none", command, mounts, "/work", ws.root, env)
        result.test = phase

        driver = _read_json(ws.results / "driver.json")
        if driver.get("install_method") in ("editable", "pythonpath"):
            result.install_method = driver["install_method"]
            if driver["install_method"] == "pythonpath":
                result.notes.append(
                    "no pyproject.toml or setup.py: repo put on PYTHONPATH"
                    if driver.get("install_log") == NO_PROJECT
                    else "offline editable install failed; used PYTHONPATH instead"
                )

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
                if phase.exit_code == 5:
                    result.status, result.reason = "unverified", "pytest collected no tests"
                    return
                output = _without_driver_lines(phase.stderr + "\n" + phase.stdout)
                result.runner_error = pytest_error(output)
                result.runner_output = output.strip()[-RUNNER_OUTPUT_CHARS:]
                usage = " (usage error)" if phase.exit_code == PYTEST_USAGE_ERROR else ""
                result.status = "error"
                result.reason = f"pytest exited {phase.exit_code}{usage} without a report" + (
                    f": {result.runner_error}" if result.runner_error else ""
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

    def _smoke(
        self, ws: _Workspace, image: str, check: SmokeCheck, plan: InstallPlan
    ) -> SmokeResult:
        """Run the migration's smoke check against the installed dependencies: no network, and
        no repo code mounted."""
        (ws.tooling / "smoke_check.py").write_text(check.code)
        mounts = [
            Mount(ws.deps, "/deps", writable=False),
            Mount(ws.tooling, "/apifix", writable=False),
        ]
        command = ["python", "/apifix/driver.py", "smoke", check.distribution]
        phase = self._phase("smoke", image, "none", command, mounts, None, ws.root)
        line = next(
            (ln for ln in reversed(phase.stdout.splitlines()) if ln.startswith(SMOKE_MARKER)), None
        )
        try:
            data = json.loads(line[len(SMOKE_MARKER) :]) if line else None
        except ValueError:
            data = None
        if phase.status != "ok" or not isinstance(data, dict):
            tail = (phase.stdout + phase.stderr)[-2000:]
            return SmokeResult("error", check.description, tail, check.distribution)
        installed = data.get("installed") or {}
        requires = {
            str(k): (str(v) if v is not None else None)
            for k, v in (installed.get("requires") or {}).items()
        }
        names = {normalize_name(check.distribution), *map(normalize_name, requires)}
        pins = {line: name for name, line in exact_pins(plan).items() if name in names}
        required = {r.split(";", 1)[0].strip() for r in plan.requirements}

        def sources(line: str, name: str) -> list[str]:
            # a pin no requirement states is a lockfile constraint
            if line in required or not plan.lockfile:
                return list(plan.declared.get(name, ()))
            return [plan.lockfile]

        return SmokeResult(
            "ok" if data.get("ok") else "failed",
            check.description,
            str(data.get("output", "")),
            check.distribution,
            installed.get("version"),
            requires,
            sorted(pins),
            {line: sources(line, name) for line, name in sorted(pins.items())},
        )

    def _phase(
        self,
        name: Literal["install", "test", "smoke"],
        image: str,
        network: Literal["bridge", "none"],
        command: list[str],
        mounts: list[Mount],
        workdir: str | None,
        workspace: Path,
        env: Mapping[str, str] | None = None,
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
            extra_env={**TEST_PHASE_ENV, **(env or {})} if name == "test" else env,
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


def _tests_run(result: SandboxResult) -> int:
    """Tests that were collected and ran or were skipped (a collection error is not one)."""
    if result.test is None or result.status == "error":
        return 0
    return max(0, result.counts.total - result.counts.errors)


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
        self,
        sandbox: Sandbox,
        repo_path: str | os.PathLike[str],
        *,
        coverage: bool,
        env: Mapping[str, str] | None = None,
        smoke: SmokeCheck | None = None,
        tests: TestPlan | None = None,
        original: str | os.PathLike[str] | None = None,
        target: LibraryTarget | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.original = Path(original).resolve() if original is not None else None
        self.target = target
        self.env = dict(env or {})
        for key, value in self.env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in value:
                raise ValueError(f"invalid sandbox environment variable {key!r}")
        self.repo = Path(repo_path).resolve()
        if not self.repo.is_dir():
            raise NotADirectoryError(f"not a directory: {self.repo}")
        self.coverage = coverage
        python = detect_python_version(self.repo, sandbox.config.default_python)
        self.smoke = smoke
        self.tests = tests or detect_tests(self.repo)
        self.base = SandboxResult(
            status="error",
            reason="",
            python_version=python.version,
            python_version_source=python.source,
            runner=self.tests.runner,
            test_command=list(self.tests.command),
            env_placeholders=sorted(self.env),
            import_paths=list(self.tests.import_paths),
            notes=list(self.tests.notes),
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
        plan = build_install_plan(
            self.repo,
            self.tests.runner,
            override=self.sandbox.config.requirements_override,
            original=self.original,
            target=self.target,
            python=base.python_version,
        )
        if self.coverage and "coverage" not in plan.requirements:
            plan = dataclasses.replace(plan, requirements=(*plan.requirements, "coverage"))
        base.dependency_sources = list(plan.sources)
        base.lockfile = plan.lockfile
        base.notes += plan.notes
        base.declared = {name: list(where) for name, where in plan.declared.items()}
        base.not_installed = list(plan.not_installed)
        base.requirements_override = plan.override
        base.override_replaced = list(plan.replaced)
        base.override_upgraded = list(plan.upgraded)
        base.dropped_sources = list(plan.dropped_sources)

        usable, why = self.sandbox.docker.available()
        if not usable:
            base.reason = why
            return
        self._workspace = _Workspace(self.repo, plan)
        self._image = self.sandbox._install(self._workspace, plan, base)
        if self._image is not None and self.smoke is not None:
            # lockfile pins the install dropped (retried without them) held nothing back
            used = plan if base.lockfile_applied else dataclasses.replace(plan, constraints=())
            base.smoke = self.sandbox._smoke(self._workspace, self._image, self.smoke, used)

    def _retry_without_import_paths(self, first: SandboxResult, measure: bool) -> SandboxResult:
        """Nothing collected with the import dirs apifix guessed: run once more without them and
        keep whichever run collected tests (the run with them if neither did)."""
        assert self._workspace is not None and self._image is not None
        plain = dataclasses.replace(self.tests, import_paths=(), import_paths_guessed=False)
        retry = copy.deepcopy(self.base)
        retry.import_paths = []
        retry.notes = [n for n in retry.notes if n not in self.tests.notes]
        self._workspace.reset_work(self.repo)
        self.sandbox._test(self._workspace, self._image, plain, retry, measure, self.env)
        paths = ", ".join(f"{p}/" for p in self.tests.import_paths)
        if _tests_run(retry) > 0:
            retry.notes.append(
                f"no tests collected with {paths} on the import path; ran again without it "
                "and used that run"
            )
            retry.retried_without_import_paths = True
            return retry
        first.notes.append(
            f"no tests collected with {paths} on the import path, or without it; kept the run "
            "with it"
        )
        first.retried_without_import_paths = True
        return first

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
            self.sandbox._test(self._workspace, self._image, self.tests, result, measure, self.env)
            if self.tests.import_paths_guessed and _tests_run(result) == 0:
                result = self._retry_without_import_paths(result, measure)
            return result
        finally:
            # This run's cost: the (shared) image pull and install plus its own test phase.
            phases = [*result.install_attempts, *([result.test] if result.test else [])]
            result.duration_s = result.image_pull_s + sum(p.duration_s for p in phases)
