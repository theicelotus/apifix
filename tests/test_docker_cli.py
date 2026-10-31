from __future__ import annotations

import io
import os
import stat
import textwrap

import pytest

from apifix.engine.docker import DockerCLI, _BoundedCapture


def test_bounded_capture_keeps_head_and_tail():
    data = b"H" * 10 + b"m" * 1000 + b"T" * 10
    cap = _BoundedCapture(io.BufferedReader(io.BytesIO(data)), head=10, tail=10)
    cap.thread.join()
    assert cap.text() == "H" * 10 + "\n... [1000 bytes truncated] ...\n" + "T" * 10


def test_bounded_capture_small_output_untouched():
    cap = _BoundedCapture(io.BufferedReader(io.BytesIO(b"hello")), head=10, tail=10)
    cap.thread.join()
    assert cap.text() == "hello"


@pytest.fixture
def fake_docker(tmp_path):
    """A stand-in `docker` executable: prints its args and environment, or sleeps."""
    script = tmp_path / "docker"
    script.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$2" = "sleep" ]; then sleep 30; fi
            echo "args: $*"
            env
            echo "to stderr" >&2
            exit 3
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_run_passes_args_and_minimal_env(fake_docker, monkeypatch):
    monkeypatch.setenv("APIFIX_CANARY_SECRET", "s3cr3t-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    result = DockerCLI(fake_docker).run(["--network", "none", "img"], timeout_s=10)
    assert result.exit_code == 3 and not result.timed_out
    assert "args: run --network none img" in result.stdout
    assert "s3cr3t-value" not in result.stdout
    assert "sk-should-not-leak" not in result.stdout
    assert "to stderr" in result.stderr


def test_run_timeout_kills_client(fake_docker):
    result = DockerCLI(fake_docker).run(["sleep"], timeout_s=0.5)
    assert result.timed_out and result.exit_code is None
    assert result.duration_s < 10


def test_pull_passes_the_image_and_minimal_env(fake_docker, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    result = DockerCLI(fake_docker).pull("python:3.11-slim", timeout_s=10)
    assert result.exit_code == 3 and not result.timed_out
    assert "args: pull python:3.11-slim" in result.stdout
    assert "sk-should-not-leak" not in result.stdout


def test_pull_timeout_kills_client(fake_docker):
    result = DockerCLI(fake_docker).pull("sleep", timeout_s=0.5)
    assert result.timed_out and result.exit_code is None


def test_unavailable_binary(tmp_path):
    ok, why = DockerCLI(str(tmp_path / "no-docker")).available()
    assert not ok and "docker not usable" in why


def test_remove_container_never_raises(tmp_path):
    DockerCLI(str(tmp_path / "no-docker")).remove_container("x")


@pytest.mark.skipif(os.name != "posix", reason="posix shell script")
def test_available_reports_daemon_errors(fake_docker):
    ok, why = DockerCLI(fake_docker).available()  # the fake exits 3
    assert not ok and "docker not usable" in why
