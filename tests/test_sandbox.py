"""Sandbox orchestration against a fake Docker: no daemon needed."""

from __future__ import annotations

import json
import shutil
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apifix.engine import sandbox as sandbox_mod
from apifix.engine.docker import ProcResult
from apifix.engine.models import LibraryTarget, SmokeCheck
from apifix.engine.project import SandboxRequirements, TestPlan
from apifix.engine.sandbox import (
    CONTAINER_ENV,
    PIP_CACHE_MARKER,
    TEST_PHASE_ENV,
    DownloadStats,
    Sandbox,
    SandboxConfig,
    container_user,
    default_pip_cache_dir,
    parse_install_stats,
    prepare_pip_cache,
    pytest_error,
    run_sandbox,
)
from apifix.engine.test_results import TestCounts

CANARY = "s3cr3t-canary-value"
UNITTEST_COMMAND = ("{python}", "-m", "unittest", "discover", "-v")


# --- fake docker ----------------------------------------------------------------------------


@dataclass
class Call:
    args: list[str]
    timeout: float

    def opt(self, flag: str) -> list[str]:
        return [self.args[i + 1] for i, a in enumerate(self.args[:-1]) if a == flag]

    @property
    def name(self) -> str:
        return self.opt("--name")[0]

    @property
    def network(self) -> str:
        return self.opt("--network")[0]

    @property
    def mounts(self) -> dict[str, tuple[Path, str]]:
        out = {}
        for spec in self.opt("--volume"):
            host, container, mode = spec.split(":")
            out[container] = (Path(host), mode)
        return out

    @property
    def phase(self) -> str:
        sub = self.args[self.args.index("/apifix/driver.py") + 1]
        return sub if sub in ("install", "smoke") else "test"

    @property
    def image(self) -> str:
        return self.args[self.args.index("python") - 1]


def ok(stdout: str = "", stderr: str = "") -> ProcResult:
    return ProcResult(0, stdout, stderr, False, 0.1)


def exited(code: int, stdout: str = "", stderr: str = "") -> ProcResult:
    return ProcResult(code, stdout, stderr, False, 0.1)


TIMED_OUT = ProcResult(None, "partial", "", True, 5.0)


def smoke_output(ok: bool = True, output: str = "", version="1.0.0", requires=None) -> ProcResult:
    """What `driver.py smoke` prints."""
    installed = {"distribution": "openai", "version": version, "requires": requires or {}}
    result = {"ok": ok, "output": output, "installed": installed}
    return ProcResult(0, "APIFIX_SMOKE_RESULT " + json.dumps(result) + "\n", "", False, 0.1)


@dataclass
class FakeDocker:
    install: list[ProcResult] = field(default_factory=lambda: [ok()])
    test: Callable[[Call], ProcResult] | None = None
    smoke: Callable[[Call], ProcResult] = lambda call: smoke_output()
    usable: bool = True
    pull_result: ProcResult = field(default_factory=ok)
    calls: list[Call] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    seen_dirs: set[Path] = field(default_factory=set)
    # container name -> {mount point: files in it when the container started}
    snapshots: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    requirements: list[str] = field(default_factory=list)  # manifest seen by each install
    pulled: list[str] = field(default_factory=list)  # images docker.pull() was called with

    def available(self) -> tuple[bool, str]:
        return self.usable, "" if self.usable else "docker not usable: permission denied"

    def pull(self, image: str, timeout_s: float) -> ProcResult:
        self.pulled.append(image)
        return self.pull_result

    def run(self, args, timeout_s):
        call = Call(list(args), timeout_s)
        self.calls.append(call)
        self.seen_dirs |= {host for host, _ in call.mounts.values()}
        self.snapshots[call.name] = {
            point: sorted(p.relative_to(host).as_posix() for p in host.rglob("*"))
            for point, (host, _) in call.mounts.items()
        }
        if call.phase == "install":
            manifest = call.mounts["/manifest"][0]
            self.requirements.append((manifest / "requirements.txt").read_text())
            return self.install.pop(0)
        if call.phase == "smoke":
            return self.smoke(call)
        assert self.test is not None, "unexpected test phase"
        return self.test(call)

    def remove_container(self, name):
        self.removed.append(name)


def write_driver_json(call: Call, method: str = "editable") -> Path:
    results = call.mounts["/results"][0]
    (results / "driver.json").write_text(json.dumps({"install_method": method}))
    return results


def junit(passed: int = 0, failed: int = 0, skipped: int = 0) -> str:
    cases = [f'<testcase classname="tests.test_a" name="test_p{i}"/>' for i in range(passed)]
    cases += [
        f'<testcase classname="tests.test_a" name="test_f{i}"><failure/></testcase>'
        for i in range(failed)
    ]
    cases += [
        f'<testcase classname="tests.test_a" name="test_s{i}"><skipped/></testcase>'
        for i in range(skipped)
    ]
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


def pytest_run(passed=0, failed=0, skipped=0, exit_code=None, method="editable"):
    def behave(call: Call) -> ProcResult:
        results = write_driver_json(call, method)
        (results / "junit.xml").write_text(junit(passed, failed, skipped))
        code = exit_code if exit_code is not None else (1 if failed else 0)
        return exited(code, stdout="pytest output")

    return behave


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    files = {
        "pyproject.toml": """
            [project]
            name = "demo"
            dependencies = ["openai==0.28.1"]
            [tool.pytest.ini_options]
            """,
        "src/demo/__init__.py": "",
        "tests/test_a.py": "def test_x():\n    assert True\n",
        ".git/config": "[remote]\nurl = https://token@github.com/x/y\n",
        ".venv/lib/junk.py": "",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    return root


@pytest.fixture(autouse=True)
def canary_env(monkeypatch):
    monkeypatch.setenv("APIFIX_CANARY_SECRET", CANARY)
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)


# --- happy path and security properties -----------------------------------------------------


def test_passing_run(repo):
    docker = FakeDocker(test=pytest_run(passed=2, skipped=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed", result.reason
    assert result.counts == TestCounts(total=3, passed=2, skipped=1)
    assert len(result.tests) == 3
    assert result.install_method == "editable"
    assert result.image == "python:3.11-slim"
    assert result.python_version == "3.11"
    assert result.runner == "pytest"
    assert [p.name for p in result.install_attempts] == ["install"]
    assert result.test is not None and result.test.network == "none"
    assert result.duration_s > 0


def test_phases_have_the_right_network(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, docker=docker)
    install, test = docker.calls
    assert (install.phase, install.network) == ("install", "bridge")
    assert (test.phase, test.network) == ("test", "none")


def test_install_phase_never_sees_the_repo(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, docker=docker)
    install = docker.calls[0]
    assert set(install.mounts) == {"/manifest", "/deps", "/apifix"}
    for host, _ in install.mounts.values():
        assert not host.resolve().is_relative_to(repo.resolve())
    seen = docker.snapshots[install.name]
    assert seen["/manifest"] == ["constraints.txt", "requirements.txt"]
    assert seen["/apifix"] == ["driver.py"]
    assert seen["/deps"] == []
    assert install.mounts["/manifest"][1] == "ro,z"


def test_mount_modes(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, docker=docker)
    install, test = docker.calls
    assert install.mounts["/deps"][1] == "rw,z"
    assert test.mounts["/deps"][1] == "ro,z"  # tests cannot tamper with dependencies
    assert test.mounts["/work"][1] == "rw,z"  # the repo copy is writable
    assert test.mounts["/results"][1] == "rw,z"
    assert test.mounts["/apifix"][1] == "ro,z"
    assert test.opt("--workdir") == ["/work"]


def test_work_is_a_copy_without_vcs_or_venvs(repo):
    seen: dict[str, list[str]] = {}

    def behave(call):
        work = call.mounts["/work"][0]
        seen["files"] = sorted(p.relative_to(work).as_posix() for p in work.rglob("*"))
        return pytest_run(passed=1)(call)

    run_sandbox(repo, docker=FakeDocker(test=behave))
    assert "tests/test_a.py" in seen["files"]
    assert not any(f.startswith((".git", ".venv")) for f in seen["files"])
    assert (repo / ".git" / "config").exists()  # original untouched


def test_hardening_flags_on_every_container(repo):
    config = SandboxConfig(cpus=1.5, memory="1g", pids_limit=128)
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, config=config, docker=docker)
    for call in docker.calls:
        assert "--read-only" in call.args
        assert call.opt("--cap-drop") == ["ALL"]
        assert call.opt("--security-opt") == ["no-new-privileges"]
        assert call.opt("--cpus") == ["1.5"]
        assert call.opt("--memory") == ["1g"] and call.opt("--memory-swap") == ["1g"]
        assert call.opt("--pids-limit") == ["128"]
        label, workspace = call.opt("--label")
        assert label == "apifix=1"
        # labeled with its workspace (for `apifix cleanup`), the dir its mounts live in
        key, _, root = workspace.partition("=")
        assert key == "apifix.workspace"
        assert all(host.parent == Path(root) for host, _ in call.mounts.values())
        (user,) = call.opt("--user")
        assert not user.startswith("0:") and user != "0"
        assert "--privileged" not in call.args


def test_no_host_environment_reaches_containers(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, docker=docker)
    install, test = docker.calls
    for call in docker.calls:
        assert CANARY not in " ".join(call.args)
        assert "--env-file" not in call.args
    assert env_of(install) == CONTAINER_ENV
    assert env_of(test) == {**CONTAINER_ENV, **TEST_PHASE_ENV}


def env_of(call: Call) -> dict[str, str]:
    return dict(e.split("=", 1) for e in call.opt("--env"))


def test_setuptools_scm_version_is_pretended_in_test_phase_only(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, docker=docker)
    install, test = docker.calls
    assert env_of(test)["SETUPTOOLS_SCM_PRETEND_VERSION"] == "0.0.0"
    assert "SETUPTOOLS_SCM_PRETEND_VERSION" not in env_of(install)


def test_container_user_is_never_root(monkeypatch):
    monkeypatch.setattr(sandbox_mod.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sandbox_mod.os, "getgid", lambda: 1000)
    assert container_user() == "1000:1000"
    monkeypatch.setattr(sandbox_mod.os, "getuid", lambda: 0)
    monkeypatch.setattr(sandbox_mod.os, "getgid", lambda: 0)
    assert container_user() == "65534:65534"


def test_timeouts_per_phase(repo):
    config = SandboxConfig(install_timeout_s=111, test_timeout_s=22)
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, config=config, docker=docker)
    assert [c.timeout for c in docker.calls] == [111, 22]


def test_python_version_selects_image(repo):
    (repo / ".python-version").write_text("3.9\n")
    docker = FakeDocker(test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.image == "python:3.9-slim"
    assert {c.image for c in docker.calls} == {"python:3.9-slim"}


# --- cleanup --------------------------------------------------------------------------------


def assert_cleaned_up(docker: FakeDocker):
    assert sorted(docker.removed) == sorted(c.name for c in docker.calls)
    assert len(set(docker.removed)) == len(docker.calls)  # unique names
    for d in docker.seen_dirs:
        assert not d.exists(), f"temp dir left behind: {d}"


def test_cleanup_on_success(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    run_sandbox(repo, docker=docker)
    assert_cleaned_up(docker)


def test_cleanup_on_test_timeout(repo):
    def behave(call):
        write_driver_json(call, "pythonpath")  # written before the tests started
        return TIMED_OUT

    docker = FakeDocker(test=behave)
    result = run_sandbox(repo, docker=docker)
    assert result.status == "timeout"
    assert result.install_method == "pythonpath"
    assert result.test is not None and result.test.status == "timeout"
    assert_cleaned_up(docker)


def test_cleanup_when_docker_raises(repo):
    def behave(call):
        raise KeyboardInterrupt

    docker = FakeDocker(test=behave)
    with pytest.raises(KeyboardInterrupt):
        run_sandbox(repo, docker=docker)
    assert_cleaned_up(docker)


def test_install_timeout_skips_tests(repo):
    docker = FakeDocker(install=[TIMED_OUT])
    result = run_sandbox(repo, docker=docker)
    assert result.status == "timeout"
    assert result.test is None
    assert len(docker.calls) == 1
    assert_cleaned_up(docker)


# --- image pull -------------------------------------------------------------------------------


def test_image_is_pulled_before_the_install_phase(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed"
    assert docker.pulled == ["python:3.11-slim"]
    assert result.image_pull_s == pytest.approx(0.1)  # ok()'s duration_s
    assert result.duration_s > result.image_pull_s


def test_a_failed_or_slow_pull_is_not_fatal(repo):
    docker = FakeDocker(test=pytest_run(passed=1), pull_result=TIMED_OUT)
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed"
    assert result.image_pull_s == pytest.approx(5.0)
    assert any("pulling python:3.11-slim exceeded" in n for n in result.notes)


def test_switching_images_on_a_build_error_pulls_the_new_one(repo):
    build_error = exited(1, stderr="error: command 'gcc' failed: No such file or directory")
    docker = FakeDocker(install=[build_error, ok()], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed"
    assert docker.pulled == ["python:3.11-slim", "python:3.11"]


# --- install retries ------------------------------------------------------------------------


def test_build_error_retries_on_full_image(repo):
    build_error = exited(1, stderr="error: command 'gcc' failed: No such file or directory")
    docker = FakeDocker(install=[build_error, ok()], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed"
    assert [p.image for p in result.install_attempts] == ["python:3.11-slim", "python:3.11"]
    assert result.image == "python:3.11"
    assert docker.calls[-1].image == "python:3.11"  # tests run where the install worked
    assert any("retrying with python:3.11" in n for n in result.notes)


def test_build_error_on_full_image_gives_up(repo):
    build_error = exited(1, stderr="Failed building wheel for psycopg2")
    docker = FakeDocker(install=[build_error, build_error])
    result = run_sandbox(repo, docker=docker)
    assert (result.status, result.reason) == ("error", "dependency install failed")
    assert len(result.install_attempts) == 2
    assert result.test is None


def test_lockfile_used_then_dropped_on_conflict(repo):
    (repo / "requirements.txt").write_text("openai==0.28.1\nrequests==2.31.0\n")
    conflict = exited(1, stderr="ERROR: ResolutionImpossible: for help visit ...")
    docker = FakeDocker(install=[conflict, ok()], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.lockfile == "requirements.txt (fully pinned)"
    assert [p.constraints_applied for p in result.install_attempts] == [True, False]
    assert result.lockfile_applied is False
    first, second = docker.calls[0], docker.calls[1]
    assert first.args[-1] == "--constraints" and second.args[-1] == "install"
    assert result.status == "passed"


def test_lockfile_applied(repo):
    (repo / "requirements.txt").write_text("openai==0.28.1\n")
    docker = FakeDocker(test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.lockfile_applied is True
    assert result.install_attempts[0].constraints_applied is True


def test_plain_install_failure_is_not_retried(repo):
    docker = FakeDocker(install=[exited(1, stderr="ERROR: Invalid requirement")])
    result = run_sandbox(repo, docker=docker)
    assert result.status == "error" and len(result.install_attempts) == 1
    assert result.install_retried is False


@pytest.mark.parametrize(
    "stderr",
    [
        # pip's network failure ends in "No matching distribution found" as well
        "WARNING: Retrying (Retry(total=4)) after connection broken by "
        "'NewConnectionError(...)': /simple/openai/\n"
        "ERROR: No matching distribution found for openai>=1.55.3,<3",
        "ERROR: HTTP error 503 while getting https://files.pythonhosted.org/...",
        "Killed",  # no recognised marker: the cause is unknown, so it may be transient
    ],
)
def test_transient_install_failure_is_retried_once(repo, stderr):
    docker = FakeDocker(install=[exited(1, stderr=stderr), ok()], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed" and result.install_retried is True
    assert [p.status for p in result.install_attempts] == ["failed", "ok"]
    assert "dependency install failed (network or unknown cause); retried 1 time(s)" in result.notes


def test_no_release_found_counts_as_a_network_failure(repo):
    # what pip prints when the index could not be reached, though it reads like a bad pin
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement openai==0.28.1 "
        "(from versions: none)\nERROR: No matching distribution found for openai==0.28.1"
    )
    docker = FakeDocker(install=[exited(1, stderr=stderr), ok()], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed" and result.install_retried is True


def test_a_version_that_does_not_exist_is_not_retried(repo):
    stderr = (
        "ERROR: Could not find a version that satisfies the requirement openai==9.9 (from "
        "versions: 0.1.0, 1.0.0)\nERROR: No matching distribution found for openai==9.9"
    )
    result = run_sandbox(repo, docker=FakeDocker(install=[exited(1, stderr=stderr)]))
    assert result.status == "error" and result.install_retried is False


def test_transient_install_failure_is_retried_up_to_three_times(repo):
    flaky = exited(1, stderr="ReadTimeoutError: HTTPSConnectionPool(host='pypi.org')")
    docker = FakeDocker(install=[flaky, flaky, flaky, flaky])
    result = run_sandbox(repo, docker=docker)
    assert (result.status, result.reason) == ("error", "dependency install failed")
    assert len(result.install_attempts) == 4 and result.install_retried is True
    assert "dependency install failed (network or unknown cause); retried 3 time(s)" in result.notes


def test_transient_install_failure_succeeds_on_a_later_retry(repo):
    flaky = exited(1, stderr="ReadTimeoutError: HTTPSConnectionPool(host='pypi.org')")
    docker = FakeDocker(install=[flaky, flaky, flaky, ok()], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed"
    assert len(result.install_attempts) == 4 and result.install_retried is True
    assert "dependency install failed (network or unknown cause); retried 3 time(s)" in result.notes


def test_env_placeholders_reach_the_test_phase_only(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    placeholder = {"OPENAI_API_KEY": "sk-placeholder"}
    result = Sandbox(docker=docker).run(repo, env=placeholder)
    install, test = docker.calls
    assert env_of(install) == CONTAINER_ENV
    assert env_of(test) == {**CONTAINER_ENV, **TEST_PHASE_ENV, **placeholder}
    assert CANARY not in " ".join(test.args)  # the host's real key never
    assert result.env_placeholders == ["OPENAI_API_KEY"]


@pytest.mark.parametrize("env", [{"BAD NAME": "x"}, {"A": "line\nbreak"}])
def test_invalid_env_placeholders_are_refused(repo, env):
    with pytest.raises(ValueError, match="invalid sandbox environment variable"):
        Sandbox(docker=FakeDocker()).session(repo, env=env)


def test_docker_run_failure_is_error(repo):
    docker = FakeDocker(install=[exited(125, stderr="Unable to find image")])
    result = run_sandbox(repo, docker=docker)
    assert result.status == "error"
    assert result.install_attempts[0].status == "error"


# --- statuses -------------------------------------------------------------------------------


def test_no_tests_is_unverified_without_docker(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    docker = FakeDocker(usable=False)
    result = run_sandbox(tmp_path, docker=docker)
    assert (result.status, result.reason) == ("unverified", "no test files found")
    assert docker.calls == []


def test_failed_tests(repo):
    result = run_sandbox(repo, docker=FakeDocker(test=pytest_run(passed=3, failed=2)))
    assert result.status == "failed"
    assert result.counts == TestCounts(total=5, passed=3, failed=2)
    assert result.reason == "2 failed, 0 errors"


def test_all_skipped_is_unverified(repo):
    result = run_sandbox(repo, docker=FakeDocker(test=pytest_run(skipped=4)))
    assert (result.status, result.reason) == ("unverified", "all 4 tests skipped")


def test_no_tests_collected_is_unverified(repo):
    def behave(call):
        write_driver_json(call)
        return exited(5)

    result = run_sandbox(repo, docker=FakeDocker(test=behave))
    assert (result.status, result.reason) == ("unverified", "pytest collected no tests")


def test_pytest_crash_without_report_is_error(repo):
    def behave(call):
        write_driver_json(call)
        return exited(4, stderr="ERROR: usage")

    result = run_sandbox(repo, docker=FakeDocker(test=behave))
    assert result.status == "error"


CONFTEST_STDERR = """[apifix] repo install method: editable
ImportError while loading conftest '/work/tests/conftest.py'.
tests/conftest.py:3: in <module>
    import pandas as pd
/deps/venv/lib/python3.11/site-packages/pandas/_libs/__init__.py:13: in <module>
    from pandas._libs.interval import Interval
pandas/_libs/interval.pyx:1: in init pandas._libs.interval
    ???
E   ValueError: numpy.dtype size changed, may indicate binary incompatibility. Expected 96 \
from C header, got 88 from PyObject
"""
CONFTEST_ERROR = (
    "ImportError while loading conftest 'tests/conftest.py': ValueError: numpy.dtype size "
    "changed, may indicate binary incompatibility. Expected 96 from C header, got 88 from "
    "PyObject"
)


def test_pytest_usage_error_keeps_pytests_message(repo):
    def behave(call):
        write_driver_json(call)
        return exited(4, stderr=CONFTEST_STDERR)

    result = run_sandbox(repo, docker=FakeDocker(test=behave))
    assert result.status == "error"
    assert result.reason == f"pytest exited 4 (usage error) without a report: {CONFTEST_ERROR}"
    assert result.runner_error == CONFTEST_ERROR
    assert "[apifix]" not in result.runner_output
    assert result.runner_output.endswith("got 88 from PyObject")


@pytest.mark.parametrize(
    ("output", "error"),
    [
        (CONFTEST_STDERR, CONFTEST_ERROR),
        (
            "ERROR: usage: pytest [options] [file_or_dir] [file_or_dir] [...]\n"
            "pytest: error: unrecognized arguments: --cov=genai --cov-report=xml\n"
            "  inifile: /work/pyproject.toml\n  rootdir: /work\n",
            "pytest: error: unrecognized arguments: --cov=genai --cov-report=xml",
        ),
        (
            "ERROR: file or directory not found: /work/tests/missing.py\n\n",
            "ERROR: file or directory not found: tests/missing.py",
        ),
        (
            "Traceback (most recent call last):\n  ...\n"
            "E   ModuleNotFoundError: No module named 'xdist'\nsomething else\n",
            "ModuleNotFoundError: No module named 'xdist'",
        ),
        ("INTERNALERROR> boom\n", "INTERNALERROR> boom"),
        ("\n  \n", None),
    ],
)
def test_pytest_error_picks_the_line_that_says_why(output, error):
    assert pytest_error(output) == error


def test_pytest_error_is_clipped():
    error = pytest_error("ERROR: " + "x" * 2_000)
    assert error is not None and len(error) == 500 and error.endswith("...")


def test_oom_kill_is_error(repo):
    def behave(call):
        write_driver_json(call)
        return exited(137)

    result = run_sandbox(repo, docker=FakeDocker(test=behave))
    assert result.status == "error" and "out of memory" in result.reason


def test_driver_never_started_is_error(repo):
    result = run_sandbox(repo, docker=FakeDocker(test=lambda call: exited(1)))
    assert (result.status, result.reason) == ("error", "the test phase did not start")


def test_pythonpath_fallback_is_recorded(repo):
    result = run_sandbox(repo, docker=FakeDocker(test=pytest_run(passed=1, method="pythonpath")))
    assert result.install_method == "pythonpath"
    assert any("PYTHONPATH" in n for n in result.notes)


def test_unittest_runner(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("import unittest\n")

    def behave(call):
        write_driver_json(call, "pythonpath")
        return exited(
            1,
            stderr="test_a (test_a.T.test_a) ... ok\ntest_b (test_a.T.test_b) ... FAIL\n"
            "Ran 2 tests in 0.01s\n\nFAILED (failures=1)\n",
        )

    # Detection always picks pytest; unittest only runs when a caller passes such a plan.
    plan = TestPlan("unittest", ("tests/test_a.py",), UNITTEST_COMMAND, "explicit")
    result = Sandbox(docker=FakeDocker(test=behave)).run(tmp_path, tests=plan)
    assert result.runner == "unittest"
    assert result.status == "failed"
    assert result.counts == TestCounts(total=2, passed=1, failed=1)


def test_docker_unavailable(repo):
    result = run_sandbox(repo, docker=FakeDocker(usable=False))
    assert result.status == "error" and "permission denied" in result.reason


def test_not_a_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        run_sandbox(tmp_path / "missing", docker=FakeDocker())


# --- sessions -------------------------------------------------------------------------------


def test_session_installs_once_and_gives_each_run_a_fresh_copy(repo):
    def behave(call):
        work = call.mounts["/work"][0]
        leftover = (work / "leftover.txt").exists()
        (work / "leftover.txt").write_text("from a previous run")
        result = pytest_run(passed=1)(call)
        return exited(result.exit_code, stdout=f"leftover={leftover}")

    docker = FakeDocker(test=behave)
    with Sandbox(docker=docker).session(repo) as session:
        first, second = session.run_tests(), session.run_tests()
    assert [c.phase for c in docker.calls] == ["install", "test", "test"]
    assert first.test.stdout == "leftover=False"
    assert second.test.stdout == "leftover=False"
    assert first.status == second.status == "passed"
    assert len(first.install_attempts) == len(second.install_attempts) == 1
    assert_cleaned_up(docker)


def coverage_run(call):
    results = call.mounts["/results"][0]
    behave = pytest_run(passed=1)
    proc = behave(call)
    if "--coverage" in call.args:
        (results / "coverage.json").write_text(
            json.dumps(
                {"files": {"src/demo/__init__.py": {"executed_lines": [1], "missing_lines": [2]}}}
            )
        )
    return proc


def test_coverage_session(repo):
    docker = FakeDocker(test=coverage_run)
    with Sandbox(docker=docker).session(repo, coverage=True) as session:
        measured = session.run_tests()
        plain = session.run_tests(coverage=False)
    assert "coverage" in docker.requirements[0].splitlines()
    assert "--coverage" in docker.calls[1].args and "--coverage" not in docker.calls[2].args
    assert measured.coverage["src/demo/__init__.py"].executed == {1}
    assert plain.coverage is None


def test_coverage_needs_a_coverage_session(repo):
    docker = FakeDocker(test=coverage_run)
    with Sandbox(docker=docker).session(repo) as session:
        result = session.run_tests(coverage=True)  # coverage was never installed
    assert "coverage" not in docker.requirements[0].splitlines()
    assert "--coverage" not in docker.calls[1].args
    assert result.coverage is None


def test_missing_coverage_report_is_noted(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    result = Sandbox(docker=docker).run(repo, coverage=True)
    assert result.coverage is None
    assert any("no report" in n for n in result.notes)


def test_session_without_tests_never_touches_docker(tmp_path):
    docker = FakeDocker(usable=False)
    with Sandbox(docker=docker).session(tmp_path) as session:
        assert session.run_tests().status == "unverified"
        assert session.run_tests().status == "unverified"
    assert docker.calls == []


# --- test layouts: explicit test files, import paths ------------------------------------------


@pytest.fixture
def flat_service(tmp_path) -> Path:
    """Synthetic layout: requirements at the root, code and a unittest file side by side in a
    package dir, the test importing the code by bare name."""
    root = tmp_path / "flat"
    files = {
        "requirements.txt": "openai==0.28.1\ntyping_extensions==4.5.0\nflask==2.2.3\n",
        "svc/__init__.py": "",
        "svc/server.py": "def reply():\n    return 1\n",
        "svc/test_server.py": "import unittest\nfrom server import reply\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_import_paths_and_explicit_files_reach_the_driver(flat_service):
    docker = FakeDocker(test=pytest_run(passed=1))
    result = run_sandbox(flat_service, docker=docker)
    (test_call,) = [c for c in docker.calls if c.phase == "test"]
    driver_args = test_call.args[test_call.args.index("/apifix/driver.py") + 1 :]
    assert driver_args[:4] == ["test", "--path", "svc", "--"]
    assert driver_args[-1] == "svc/test_server.py"
    assert result.import_paths == ["svc"]
    assert result.test_command[-1] == "svc/test_server.py"
    assert (
        "added svc/ to the import path: svc/test_server.py imports server from it" in result.notes
    )


def test_note_when_there_is_nothing_to_install(flat_service):
    def behave(call):
        results = call.mounts["/results"][0]
        driver = {"install_method": "pythonpath", "install_log": "no pyproject.toml or setup.py"}
        (results / "driver.json").write_text(json.dumps(driver))
        (results / "junit.xml").write_text(junit(passed=1))
        return ok()

    result = run_sandbox(flat_service, docker=FakeDocker(test=behave))
    assert "no pyproject.toml or setup.py: repo put on PYTHONPATH" in result.notes
    assert not any("editable install failed" in n for n in result.notes)


# --- smoke check ------------------------------------------------------------------------------

SMOKE = SmokeCheck("openai", "import openai\nopenai.OpenAI(api_key='x')\n", "make a client")


def test_smoke_check_runs_once_without_network_or_repo(flat_service):
    docker = FakeDocker(test=pytest_run(passed=1))
    with Sandbox(docker=docker).session(flat_service, smoke=SMOKE) as session:
        first, second = session.run_tests(), session.run_tests()
    smoke_calls = [c for c in docker.calls if c.phase == "smoke"]
    assert len(smoke_calls) == 1
    (call,) = smoke_calls
    assert call.network == "none"
    assert {k: mode for k, (_, mode) in call.mounts.items()} == {
        "/deps": "ro,z",
        "/apifix": "ro,z",
    }
    assert "smoke_check.py" in docker.snapshots[call.name]["/apifix"]
    assert call.args[-2:] == ["smoke", "openai"]
    assert first.smoke == second.smoke and first.smoke.status == "ok"
    assert first.smoke.description == "make a client"


def test_smoke_failure_records_versions_and_limiting_pins(flat_service):
    out = smoke_output(
        ok=False,
        output="Traceback ...\nTypeError: Client.__init__() got an unexpected keyword argument "
        "'proxies'\n",
        version="1.5.0",
        requires={"httpx": "0.28.1", "typing-extensions": "4.5.0", "pydantic": "1.10.26"},
    )
    docker = FakeDocker(test=pytest_run(passed=1), smoke=lambda call: out)
    smoke = Sandbox(docker=docker).run(flat_service, smoke=SMOKE).smoke
    assert smoke.status == "failed"
    assert (smoke.distribution, smoke.version) == ("openai", "1.5.0")
    assert smoke.requires["httpx"] == "0.28.1"
    # flask==2.2.3 is pinned too, but is not openai or one of its requirements
    assert smoke.pins == ["openai==0.28.1", "typing_extensions==4.5.0"]
    assert "proxies" in smoke.output


def test_smoke_pins_record_where_they_are_declared(flat_service):
    out = smoke_output(
        ok=False,
        output="AttributeError: module 'openai' has no attribute 'OpenAI'\n",
        version="0.28.1",
        requires={"typing-extensions": "4.5.0"},
    )
    docker = FakeDocker(test=pytest_run(passed=1), smoke=lambda call: out)
    smoke = Sandbox(docker=docker).run(flat_service, smoke=SMOKE).smoke
    assert smoke.pin_sources == {
        "openai==0.28.1": ["requirements.txt"],
        "typing_extensions==4.5.0": ["requirements.txt"],
    }


LOCKED_PROJECT = {
    "pyproject.toml": '[tool.poetry.dependencies]\nopenai = ">=1.55.3,<3"\n',
    "poetry.lock": '[[package]]\nname = "tqdm"\nversion = "4.65.0"\n',
    "tests/test_a.py": "def test_x():\n    assert True\n",
}
HELD_BACK = smoke_output(
    ok=False,
    output="TypeError: Client.__init__() got an unexpected keyword argument 'proxies'\n",
    version="1.39.0",
    requires={"httpx": "0.28.1", "tqdm": "4.65.0"},
)


def _locked_project(tmp_path) -> Path:
    root = tmp_path / "locked"
    for rel, text in LOCKED_PROJECT.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_smoke_pins_from_the_lockfile_name_the_lockfile(tmp_path):
    docker = FakeDocker(test=pytest_run(passed=1), smoke=lambda call: HELD_BACK)
    smoke = Sandbox(docker=docker).run(_locked_project(tmp_path), smoke=SMOKE).smoke
    assert smoke.pins == ["tqdm==4.65.0"]
    assert smoke.pin_sources == {"tqdm==4.65.0": ["poetry.lock"]}


def test_smoke_pins_leave_out_a_lockfile_the_install_dropped(tmp_path):
    conflict = exited(1, stderr="ERROR: ResolutionImpossible: for help visit ...")
    docker = FakeDocker(
        install=[conflict, ok()], test=pytest_run(passed=1), smoke=lambda call: HELD_BACK
    )
    result = Sandbox(docker=docker).run(_locked_project(tmp_path), smoke=SMOKE)
    assert result.lockfile_applied is False
    assert result.smoke.pins == []


def test_fixed_run_with_sandbox_requirements_gets_the_target_for_an_old_library_pin(
    flat_service, tmp_path
):
    # SuspensePerception: the user's file pins openai==0.28.1 for the original code, and the
    # fix did not change openai's declaration
    override = SandboxRequirements("/home/u/reqs.txt", "0" * 64, ("openai==0.28.1", "flask"))
    fixed = tmp_path / "fixed"
    shutil.copytree(flat_service, fixed)
    (fixed / "svc" / "server.py").write_text("def reply():\n    return 2\n")
    docker = FakeDocker(test=pytest_run(passed=1))
    sandbox = Sandbox(SandboxConfig(requirements_override=override), docker=docker)
    target = LibraryTarget("openai", "1.0", ">=1.55.3,<3")
    result = sandbox.run(fixed, original=flat_service, target=target)
    assert result.override_upgraded == ["openai==0.28.1 -> openai>=1.55.3,<3"]
    (manifest,) = docker.requirements
    assert "openai>=1.55.3,<3\n" in manifest and "openai==0.28.1" not in manifest
    # without `original` (the baseline) the user's line stays
    docker = FakeDocker(test=pytest_run(passed=1))
    sandbox = Sandbox(SandboxConfig(requirements_override=override), docker=docker)
    assert sandbox.run(flat_service, target=target).override_upgraded == []
    assert "openai==0.28.1\n" in docker.requirements[0]


@pytest.mark.parametrize(
    "proc", [exited(125, stderr="docker: error"), ok(stdout="no marker here\n")]
)
def test_smoke_check_that_could_not_run_is_an_error(flat_service, proc):
    docker = FakeDocker(test=pytest_run(passed=1), smoke=lambda call: proc)
    result = Sandbox(docker=docker).run(flat_service, smoke=SMOKE)
    assert result.smoke.status == "error"
    assert result.status == "passed"  # the tests still ran


def test_no_smoke_phase_without_a_check(flat_service):
    docker = FakeDocker(test=pytest_run(passed=1))
    assert run_sandbox(flat_service, docker=docker).smoke is None
    assert all(c.phase != "smoke" for c in docker.calls)


def test_no_smoke_phase_when_the_install_failed(flat_service):
    docker = FakeDocker(install=[exited(1, stderr="ResolutionImpossible")] * 4)
    result = Sandbox(docker=docker).run(flat_service, smoke=SMOKE)
    assert result.smoke is None and all(c.phase != "smoke" for c in docker.calls)


# --- persistent pip cache -------------------------------------------------------------------


def stats_line(**counts) -> str:
    data = {"downloaded": 0, "downloaded_bytes": 0, "cached": 0, "cached_bytes": 0, **counts}
    return "APIFIX_INSTALL_STATS " + json.dumps(data) + "\n"


def test_pip_cache_is_off_by_default(repo):
    docker = FakeDocker(test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    install = docker.calls[0]
    assert "/pipcache" not in install.mounts
    assert env_of(install)["PIP_NO_CACHE_DIR"] == "1"
    assert result.pip_cache is False


def test_pip_cache_is_mounted_in_the_install_phase_only(repo, tmp_path):
    cache = tmp_path / "cache" / "pip"
    config = SandboxConfig(pip_cache_dir=cache)
    smoke = SmokeCheck("openai", "import openai", "import openai\n")
    docker = FakeDocker(test=pytest_run(passed=1))
    result = Sandbox(config, docker).run(repo, smoke=smoke)
    install, smoke_call, test = docker.calls
    assert install.mounts["/pipcache"] == (cache.resolve(), "rw,z")
    env = env_of(install)
    assert env["PIP_CACHE_DIR"] == "/pipcache"
    assert "PIP_NO_CACHE_DIR" not in env  # pip treats any value as "no cache"
    for call in (smoke_call, test):
        assert "/pipcache" not in call.mounts
        assert "PIP_CACHE_DIR" not in env_of(call)
        assert call.network == "none"
    assert (cache / PIP_CACHE_MARKER).is_file()
    assert result.pip_cache is True


def test_pip_cache_reused_by_every_install_attempt(repo, tmp_path):
    flaky = exited(1, stderr="ReadTimeoutError: HTTPSConnectionPool(host='pypi.org')")
    docker = FakeDocker(install=[flaky, ok()], test=pytest_run(passed=1))
    Sandbox(SandboxConfig(pip_cache_dir=tmp_path / "c"), docker).run(repo)
    installs = [c for c in docker.calls if c.phase == "install"]
    assert len(installs) == 2 and all("/pipcache" in c.mounts for c in installs)


def test_a_directory_apifix_did_not_create_is_never_mounted(repo, tmp_path):
    foreign = tmp_path / "home"
    (foreign / "notes.txt").parent.mkdir()
    (foreign / "notes.txt").write_text("mine")
    docker = FakeDocker(test=pytest_run(passed=1))
    result = Sandbox(SandboxConfig(pip_cache_dir=foreign), docker).run(repo)
    assert "/pipcache" not in docker.calls[0].mounts
    assert result.pip_cache is False and result.status == "passed"
    assert any("was not created by apifix" in n for n in result.notes)
    assert not (foreign / PIP_CACHE_MARKER).exists()


def test_prepare_pip_cache(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert prepare_pip_cache(empty) == (empty.resolve(), "")  # an empty dir is adopted
    assert prepare_pip_cache(empty) == (empty.resolve(), "")  # and reused
    (empty / "http").mkdir()
    assert prepare_pip_cache(empty)[0] == empty.resolve()  # has the marker
    file = tmp_path / "file"
    file.write_text("x")
    assert prepare_pip_cache(file)[0] is None
    # a symlink is judged by what it points at
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "x").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "other")
    path, why = prepare_pip_cache(tmp_path / "link")
    assert path is None and str(tmp_path / "other") in why


def test_default_pip_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("APIFIX_PIP_CACHE", str(tmp_path / "explicit"))
    assert default_pip_cache_dir() == tmp_path / "explicit"
    monkeypatch.delenv("APIFIX_PIP_CACHE")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_pip_cache_dir() == tmp_path / "xdg" / "apifix" / "pip"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert default_pip_cache_dir() == Path.home() / ".cache" / "apifix" / "pip"


def test_install_download_figures_are_recorded(repo):
    line = stats_line(downloaded=2, downloaded_bytes=7_300_000, cached=1, cached_bytes=11_000)
    docker = FakeDocker(install=[ok(stdout="Collecting six\n" + line)], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.install_attempts[0].downloads == DownloadStats(2, 7_300_000, 1, 11_000)


@pytest.mark.parametrize("stdout", ["", "APIFIX_INSTALL_STATS not json\n", stats_line()[:-10]])
def test_missing_or_broken_download_figures(stdout):
    assert parse_install_stats(stdout) is None


# --- --sandbox-requirements -------------------------------------------------------------------


def test_sandbox_requirements_replace_detection_and_keep_the_fixs_changes(repo, tmp_path):
    override = SandboxRequirements("/x/reqs.txt", "f" * 64, ("openai==0.28", "typer"))
    config = SandboxConfig(requirements_override=override)
    docker = FakeDocker(install=[ok(), ok()], test=pytest_run(passed=1))
    baseline = Sandbox(config, docker).run(repo)
    fixed = tmp_path / "fixed"
    shutil.copytree(repo, fixed)
    pyproject = fixed / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace("openai==0.28.1", "openai>=1.0"))
    after = Sandbox(config, docker).run(fixed, original=repo)

    first, second = docker.requirements
    assert first.splitlines()[:2] == ["openai==0.28", "typer"]
    assert second.splitlines()[:2] == ["typer", "openai>=1.0"]
    assert baseline.requirements_override == "--sandbox-requirements reqs.txt"
    assert baseline.dependency_sources == ["--sandbox-requirements reqs.txt"]
    assert baseline.override_replaced == []
    assert after.override_replaced == ["openai: openai==0.28 -> openai>=1.0"]
    assert after.declared["openai"] == ["pyproject.toml"]
    assert baseline.declared["openai"] == ["--sandbox-requirements reqs.txt"]


def test_detected_dependencies_are_recorded_per_run(repo):
    result = run_sandbox(repo, docker=FakeDocker(test=pytest_run(passed=1)))
    assert result.declared == {"openai": ["pyproject.toml"]}
    assert result.requirements_override is None and result.not_installed == []


# --- resolution conflicts (docs/plans/llmflows-run-1.md) ----------------------------------------

# llmflows' baseline: pyproject.toml and requirements.txt pinned tiktoken differently
PIP_CONFLICT_STDOUT = """\
Collecting openai==0.28.1 (from -r /manifest/requirements.txt (line 1))
  Using cached openai-0.28.1-py3-none-any.whl.metadata (11 kB)
Collecting tiktoken==0.5.1 (from -r /manifest/requirements.txt (line 2))

The conflict is caused by:
    The user requested tiktoken==0.5.1
    The user requested tiktoken==0.3.3

To fix this you could try to:
1. loosen the range of package versions you've specified
2. remove package versions to allow pip attempt to solve the dependency conflict
"""
PIP_CONFLICT_STDERR = """\
ERROR: Cannot install tiktoken==0.3.3 and tiktoken==0.5.1 because these package versions have \
conflicting dependencies.
ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-\
resolution/#dealing-with-dependency-conflicts
"""


def _plan(reqs=(), constraints=()):
    from apifix.engine.project import InstallPlan

    return InstallPlan(tuple(reqs), tuple(constraints), (), None)


def test_install_conflict_lines():
    lines = sandbox_mod.install_conflict(PIP_CONFLICT_STDOUT + PIP_CONFLICT_STDERR, _plan())
    assert lines == [
        "The conflict is caused by:",
        "    The user requested tiktoken==0.5.1",
        "    The user requested tiktoken==0.3.3",
        "Cannot install tiktoken==0.3.3 and tiktoken==0.5.1 because these package versions "
        "have conflicting dependencies.",
    ]


def test_install_conflict_shows_manifest_lines_as_requirements():
    output = (
        "ERROR: Cannot install -r /manifest/requirements.txt (line 2) and httpx==0.27.0 because "
        "these package versions have conflicting dependencies.\n\n"
        "The conflict is caused by:\n"
        "    The user requested httpx==0.27.0\n"
        "    The user requested (constraint) httpx==0.23.0\n"
        "    openai 1.55.3 depends on httpx<1 and >=0.23.0\n"
        "\nAdditionally, some packages ...\n"
        "-c /manifest/constraints.txt (line 1) and -r /manifest/requirements.txt (line 9)\n"
    )
    plan = _plan(["openai>=1.55.3,<3", "httpx==0.27.0"], ["httpx==0.23.0"])
    lines = sandbox_mod.install_conflict(output, plan)
    assert lines[0].startswith("Cannot install httpx==0.27.0 and httpx==0.27.0 because")
    assert lines[1:] == [
        "The conflict is caused by:",
        "    The user requested httpx==0.27.0",
        "    The user requested (constraint) httpx==0.23.0",
        "    openai 1.55.3 depends on httpx<1 and >=0.23.0",
    ]
    assert sandbox_mod.conflict_sources(lines, {"httpx": ["requirements.txt"], "x": ["a"]}) == {
        "httpx": ["requirements.txt"]
    }


def test_a_conflict_is_not_retried_as_a_network_failure(repo):
    # a network warning in the output does not make a resolution conflict transient
    stderr = "WARNING: Retrying after connection broken by 'NewConnectionError'\n"
    failed = exited(1, PIP_CONFLICT_STDOUT, stderr + PIP_CONFLICT_STDERR)
    result = run_sandbox(repo, docker=FakeDocker(install=[failed]))
    assert (result.status, result.reason) == ("error", "dependency install failed")
    assert len(result.install_attempts) == 1 and result.install_retried is False
    assert "    The user requested tiktoken==0.3.3" in result.install_conflict


def test_a_conflict_without_a_lockfile_pin_is_not_retried_without_the_lockfile(repo):
    (repo / "requirements.txt").write_text("openai==0.28.1\nrequests==2.31.0\n")
    failed = exited(1, PIP_CONFLICT_STDOUT, PIP_CONFLICT_STDERR)
    docker = FakeDocker(install=[failed, ok()], test=pytest_run(passed=1))
    result = run_sandbox(repo, docker=docker)
    assert result.status == "error" and len(result.install_attempts) == 1
    assert (
        "pip's conflict involves no requirements.txt (fully pinned) pin; not retried without "
        "them" in result.notes
    )


def test_a_conflict_with_a_lockfile_pin_is_retried_without_the_lockfile(repo):
    (repo / "requirements.txt").write_text("openai==0.28.1\nrequests==2.31.0\n")
    stdout = PIP_CONFLICT_STDOUT.replace(
        "The user requested tiktoken==0.3.3", "The user requested (constraint) tiktoken==0.3.3"
    )
    docker = FakeDocker(
        install=[exited(1, stdout, PIP_CONFLICT_STDERR), ok()], test=pytest_run(passed=1)
    )
    result = run_sandbox(repo, docker=docker)
    assert result.status == "passed"
    assert [p.constraints_applied for p in result.install_attempts] == [True, False]
    assert result.install_conflict == []  # resolved by the retry


def test_dropped_sources_reach_the_result(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(Path(__file__).parent / "fixtures" / "mixed_access", root)
    docker = FakeDocker(test=pytest_run(passed=1))
    result = run_sandbox(root, docker=docker)
    (dropped,) = result.dropped_sources
    assert (dropped.file, dropped.kept) == ("pyproject.toml", "requirements.txt")
    assert docker.requirements[0].splitlines()[:3] == [
        "openai==0.28.1",
        "tiktoken==0.3.3",
        "six==1.15.0",
    ]
    assert "tiktoken==0.5.1" not in docker.requirements[0]


# --- guessed import paths that break collection ----------------------------------------------

GUESSED = TestPlan(
    "pytest",
    ("aipl/ops/test_ops.py",),
    ("{python}", "-m", "pytest"),
    "explicit",
    import_paths=("aipl/ops",),
    notes=("added aipl/ops/ to the import path: aipl/ops/test_ops.py imports ops_helper",),
    import_paths_guessed=True,
)


def paths_in(call: Call) -> list[str]:
    return call.opt("--path") if "--path" in call.args else []


def collects_only_without_paths(call: Call) -> ProcResult:
    """The circular import of aipl: collection crashes while the dir is on the path."""
    results = write_driver_json(call)
    if "--path" in call.args:
        return exited(2, stderr="ImportError: cannot import name 'loads' from partially init json")
    (results / "junit.xml").write_text(junit(passed=3))
    return ok("pytest output")


def test_collection_failure_retries_without_guessed_import_paths(repo):
    docker = FakeDocker(test=collects_only_without_paths)
    result = Sandbox(docker=docker).run(repo, tests=GUESSED)
    tests = [c for c in docker.calls if c.phase == "test"]
    assert [bool(c.opt("--path")) for c in tests] == [True, False]
    assert result.status == "passed"
    assert result.import_paths == []
    assert result.retried_without_import_paths
    assert any("ran again without it" in n for n in result.notes)
    assert not any(n.startswith("added aipl/ops/") for n in result.notes)


def test_retry_that_collects_nothing_keeps_the_first_run(repo):
    def nothing(call: Call) -> ProcResult:
        write_driver_json(call)
        return exited(2, stderr="boom")

    docker = FakeDocker(test=nothing)
    result = Sandbox(docker=docker).run(repo, tests=GUESSED)
    assert len([c for c in docker.calls if c.phase == "test"]) == 2
    assert result.import_paths == ["aipl/ops"]
    assert result.retried_without_import_paths
    assert any("kept the run with it" in n for n in result.notes)


def test_no_retry_when_the_guessed_paths_collected_tests(repo):
    docker = FakeDocker(test=pytest_run(passed=2))
    result = Sandbox(docker=docker).run(repo, tests=GUESSED)
    assert len([c for c in docker.calls if c.phase == "test"]) == 1
    assert result.import_paths == ["aipl/ops"]
    assert not result.retried_without_import_paths


def test_no_retry_for_import_paths_from_the_repos_config(repo):
    import dataclasses

    configured = dataclasses.replace(GUESSED, import_paths_guessed=False)
    docker = FakeDocker(test=collects_only_without_paths)
    result = Sandbox(docker=docker).run(repo, tests=configured)
    assert len([c for c in docker.calls if c.phase == "test"]) == 1
    assert result.status == "error"
    assert result.import_paths == ["aipl/ops"]
