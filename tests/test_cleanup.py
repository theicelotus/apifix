"""`apifix cleanup`: orphaned containers and stale workspaces, with a fake Docker."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from apifix import cli
from apifix.engine.cleanup import cleanup
from apifix.engine.docker import ContainerInfo, DockerError, parse_ps_output
from apifix.engine.project import build_install_plan
from apifix.engine.sandbox import (
    WORKSPACE_LOCK,
    WORKSPACE_PREFIX,
    _Workspace,
    lock_workspace,
    unlock_workspace,
)


@dataclass
class FakeDocker:
    containers: list[ContainerInfo] = field(default_factory=list)
    usable: bool = True
    stuck: set[str] = field(default_factory=set)  # ids that survive removal
    removed: list[str] = field(default_factory=list)

    def available(self):
        return self.usable, "" if self.usable else "docker not usable: permission denied"

    def list_containers(self, label):
        assert label == "apifix=1"
        return list(self.containers)

    def remove_container(self, name):
        self.removed.append(name)
        if name not in self.stuck:
            self.containers = [c for c in self.containers if c.id != name]


def workspace(tmp: Path, name: str = "abc", *, lock_file: bool = True, age: float = 3600) -> Path:
    root = tmp / f"{WORKSPACE_PREFIX}{name}"
    (root / "deps" / "venv").mkdir(parents=True)
    (root / "deps" / "venv" / "big.bin").write_bytes(b"x" * 1000)
    if lock_file:
        (root / WORKSPACE_LOCK).touch()
    old = time.time() - age
    os.utime(root, (old, old))
    return root


def container(cid: str, state: str = "exited", ws: Path | str = "") -> ContainerInfo:
    return ContainerInfo(cid, f"apifix-test-{cid}", state, str(ws))


def test_stale_workspace_is_removed(tmp_path):
    root = workspace(tmp_path)
    report = cleanup(FakeDocker(), temp_dir=tmp_path)
    assert report.removed_dirs == [str(root)]
    assert not root.exists()
    assert report.errors == []


def test_workspace_without_lock_file_is_removed_when_old(tmp_path):
    root = workspace(tmp_path, lock_file=False)
    assert cleanup(FakeDocker(), temp_dir=tmp_path).removed_dirs == [str(root)]


def test_young_workspace_is_left_alone(tmp_path):
    root = workspace(tmp_path, lock_file=False, age=0)
    report = cleanup(FakeDocker(), temp_dir=tmp_path)
    assert report.removed_dirs == []
    assert root.exists()
    assert "just created" in report.skipped[0]


def test_locked_workspace_is_in_use(tmp_path):
    root = workspace(tmp_path)
    fd = lock_workspace(root)
    try:
        report = cleanup(FakeDocker(), temp_dir=tmp_path)
    finally:
        unlock_workspace(fd)
    assert root.exists()
    assert report.removed_dirs == []
    assert "in use" in report.skipped[0]


def test_lock_of_a_killed_process_is_released(tmp_path):
    """The crash case: the owner dies without cleaning up; its lock goes with it."""
    root = workspace(tmp_path)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path\n"
            "from apifix.engine.sandbox import lock_workspace\n"
            "assert lock_workspace(Path(sys.argv[1])) is not None\n"
            "print('locked', flush=True); time.sleep(60)",
            str(root),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        assert cleanup(FakeDocker(), temp_dir=tmp_path).removed_dirs == []
        child.send_signal(signal.SIGKILL)
        child.wait()
        os.utime(root, (time.time() - 3600,) * 2)
        assert cleanup(FakeDocker(), temp_dir=tmp_path).removed_dirs == [str(root)]
    finally:
        child.kill()
        child.wait()


def test_sandbox_workspace_holds_its_lock_until_cleanup(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    ws = _Workspace(repo, build_install_plan(repo, "pytest"))
    try:
        assert ws.root.name.startswith(WORKSPACE_PREFIX)
        assert lock_workspace(ws.root) is None
    finally:
        ws.cleanup()
    assert not ws.root.exists()


def test_other_temp_dirs_and_symlinks_are_untouched(tmp_path):
    other = tmp_path / "pytest-of-me"
    other.mkdir()
    target = workspace(tmp_path / "elsewhere")
    (tmp_path / f"{WORKSPACE_PREFIX}link").symlink_to(target)
    report = cleanup(FakeDocker(), temp_dir=tmp_path)
    assert report.removed_dirs == []
    assert other.exists() and target.exists()
    assert "not owned" in report.skipped[0]


def test_orphaned_container_and_its_workspace_are_removed(tmp_path):
    elsewhere = tmp_path / "other-tmp"
    root = workspace(elsewhere)  # not under temp_dir: found through the label
    docker = FakeDocker([container("c1", "running", root)])
    report = cleanup(docker, temp_dir=tmp_path)
    assert docker.removed == ["c1"]
    assert report.removed_containers == ["apifix-test-c1 (running)"]
    assert report.removed_dirs == [str(root)]
    assert not root.exists()


def test_container_of_a_live_run_is_kept(tmp_path):
    root = workspace(tmp_path)
    fd = lock_workspace(root)
    docker = FakeDocker([container("c1", "running", root)])
    try:
        report = cleanup(docker, temp_dir=tmp_path)
    finally:
        unlock_workspace(fd)
    assert docker.removed == []
    assert root.exists()
    assert "still in progress" in report.skipped[0]


def test_container_whose_workspace_is_gone_is_removed(tmp_path):
    docker = FakeDocker([container("c1", "exited", tmp_path / f"{WORKSPACE_PREFIX}gone")])
    assert cleanup(docker, temp_dir=tmp_path).removed_containers == ["apifix-test-c1 (exited)"]


def test_container_with_foreign_workspace_label_is_kept(tmp_path):
    docker = FakeDocker([container("c1", "exited", "/home/someone/project")])
    report = cleanup(docker, temp_dir=tmp_path)
    assert docker.removed == []
    assert "not this user's" in report.skipped[0]


def test_unlabeled_containers_from_older_versions(tmp_path):
    docker = FakeDocker([container("old", "exited"), container("live", "running")])
    report = cleanup(docker, temp_dir=tmp_path)
    assert docker.removed == ["old"]
    assert report.removed_containers == ["apifix-test-old (exited)"]
    assert "docker rm -f apifix-test-live" in report.skipped[0]


def test_dry_run_removes_nothing(tmp_path):
    root = workspace(tmp_path)
    docker = FakeDocker([container("c1", "exited", root)])
    report = cleanup(docker, temp_dir=tmp_path, dry_run=True)
    assert report.removed_containers == ["apifix-test-c1 (exited)"]
    assert report.removed_dirs == [str(root)]
    assert docker.removed == [] and root.exists()


def test_container_that_will_not_go_is_an_error_and_keeps_its_workspace(tmp_path):
    root = workspace(tmp_path)
    docker = FakeDocker([container("c1", "running", root)], stuck={"c1"})
    report = cleanup(docker, temp_dir=tmp_path)
    assert report.errors == ["container apifix-test-c1 could not be removed"]
    assert report.removed_containers == []
    assert report.removed_dirs == [] and root.exists()


def test_docker_unavailable_still_cleans_temp_dirs(tmp_path):
    root = workspace(tmp_path)
    report = cleanup(FakeDocker(usable=False), temp_dir=tmp_path)
    assert report.errors == ["containers not checked: docker not usable: permission denied"]
    assert report.removed_dirs == [str(root)]


def test_docker_listing_failure_is_reported(tmp_path):
    class Broken(FakeDocker):
        def list_containers(self, label):
            raise DockerError("docker ps failed: boom")

    report = cleanup(Broken(), temp_dir=tmp_path)
    assert report.errors == ["containers not checked: docker ps failed: boom"]


def test_parse_ps_output():
    text = (
        "abc\tapifix-test-1\texited\t/tmp/apifix-sandbox-x\n"
        "def\tapifix-install-2\trunning\t\n"
        "ghi\tapifix-test-3\tcreated\t<no value>\n"
        "\n"
    )
    assert parse_ps_output(text) == [
        ContainerInfo("abc", "apifix-test-1", "exited", "/tmp/apifix-sandbox-x"),
        ContainerInfo("def", "apifix-install-2", "running", ""),
        ContainerInfo("ghi", "apifix-test-3", "created", ""),
    ]


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    docker = FakeDocker()
    monkeypatch.setattr(cli, "DockerCLI", lambda: docker)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return docker


def test_cli_cleanup(tmp_path, cli_env, capsys):
    root = workspace(tmp_path)
    cli_env.containers = [container("c1", "exited", root)]
    assert cli.main(["cleanup"]) == 0
    out = capsys.readouterr().out
    assert f"removed container apifix-test-c1 (exited)\nremoved temp dir {root}\n" in out
    assert out.endswith("removed 1 container(s), 1 temp dir(s)\n")


def test_cli_cleanup_dry_run_and_nothing_to_do(tmp_path, cli_env, capsys):
    assert cli.main(["cleanup", "--dry-run"]) == 0
    assert (
        capsys.readouterr().out == "would remove 0 container(s), 0 temp dir(s): nothing left over\n"
    )


def test_cli_cleanup_errors_exit_nonzero(cli_env, capsys):
    cli_env.usable = False
    assert cli.main(["cleanup"]) == 1
    assert "error: containers not checked" in capsys.readouterr().out


def test_unconfirmed_removal_keeps_the_workspace(tmp_path):
    root = workspace(tmp_path)

    class Flaky(FakeDocker):
        calls = 0

        def list_containers(self, label):
            self.calls += 1
            if self.calls > 1:
                raise DockerError("docker ps failed: daemon went away")
            return super().list_containers(label)

    report = cleanup(Flaky([container("c1", "running", root)]), temp_dir=tmp_path)
    assert report.errors == [
        "could not confirm container removal: docker ps failed: daemon went away"
    ]
    assert report.removed_dirs == [] and root.exists()
