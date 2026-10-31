"""Minimal Docker access via the `docker` CLI (no SDK dependency).

The sandbox talks to Docker only through the ``DockerClient`` protocol, so unit tests can use a
fake. Output is captured with a fixed memory bound: the head and tail are kept, the middle is
dropped, because a misbehaving test suite can print without limit.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import IO, Protocol

# Only what the docker CLI itself needs to reach the daemon. None of this reaches containers:
# container environments are set explicitly with -e by the sandbox.
_CLI_ENV_KEYS = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "XDG_RUNTIME_DIR",
)


@dataclass(frozen=True)
class ProcResult:
    exit_code: int | None  # None when killed by the timeout
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


@dataclass(frozen=True)
class ContainerInfo:
    id: str
    name: str
    state: str  # created, running, exited, ...
    workspace: str  # value of the apifix.workspace label ("" if absent)


class DockerError(RuntimeError):
    pass


class DockerClient(Protocol):
    def available(self) -> tuple[bool, str]:
        """(usable, reason if not)."""
        ...

    def run(self, args: Sequence[str], timeout_s: float) -> ProcResult:
        """Run `docker run <args>` in the foreground, killing the client at the timeout."""
        ...

    def pull(self, image: str, timeout_s: float) -> ProcResult:
        """Run `docker pull <image>` in the foreground, killing the client at the timeout."""
        ...

    def remove_container(self, name: str) -> None:
        """Force-remove a container if it exists. Must not raise."""
        ...

    def list_containers(self, label: str) -> list[ContainerInfo]:
        """All containers (any state) carrying `label`. Raises DockerError."""
        ...


class _BoundedCapture:
    def __init__(self, stream: IO[bytes], head: int, tail: int) -> None:
        self._stream, self._head_limit, self._tail_limit = stream, head, tail
        self.head, self.tail, self.total = bytearray(), bytearray(), 0
        self.thread = threading.Thread(target=self._pump, daemon=True)
        self.thread.start()

    def _pump(self) -> None:
        read = getattr(self._stream, "read1", self._stream.read)
        while chunk := read(65536):
            self.total += len(chunk)
            room = self._head_limit - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self.tail += chunk
                if len(self.tail) > self._tail_limit:
                    del self.tail[: len(self.tail) - self._tail_limit]

    def text(self) -> str:
        dropped = self.total - len(self.head) - len(self.tail)
        head = self.head.decode("utf-8", "replace")
        tail = self.tail.decode("utf-8", "replace")
        if dropped > 0:
            return f"{head}\n... [{dropped} bytes truncated] ...\n{tail}"
        return head + tail


class DockerCLI:
    def __init__(
        self, binary: str = "docker", head_bytes: int = 4_000, tail_bytes: int = 16_000
    ) -> None:
        self.binary = binary
        self.head_bytes = head_bytes
        self.tail_bytes = tail_bytes

    def _env(self) -> dict[str, str]:
        return {k: os.environ[k] for k in _CLI_ENV_KEYS if k in os.environ}

    def available(self) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                [self.binary, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=30,
                env=self._env(),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"docker not usable: {exc}"
        if proc.returncode != 0:
            return False, f"docker not usable: {proc.stderr.strip() or proc.stdout.strip()}"
        return True, ""

    def run(self, args: Sequence[str], timeout_s: float) -> ProcResult:
        return self._exec(["run", *args], timeout_s)

    def pull(self, image: str, timeout_s: float) -> ProcResult:
        return self._exec(["pull", image], timeout_s)

    def _exec(self, args: Sequence[str], timeout_s: float) -> ProcResult:
        start = time.monotonic()
        proc = subprocess.Popen(
            [self.binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=self._env(),
            start_new_session=True,  # own process group, so the whole client tree can be killed
        )
        assert proc.stdout is not None and proc.stderr is not None
        out = _BoundedCapture(proc.stdout, self.head_bytes, self.tail_bytes)
        err = _BoundedCapture(proc.stderr, self.head_bytes, self.tail_bytes)
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Killing the client does not stop the container; the caller removes it.
            timed_out = True
        finally:
            if proc.poll() is None:  # timed out, or interrupted (e.g. KeyboardInterrupt)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            out.thread.join(timeout=10)
            err.thread.join(timeout=10)
        return ProcResult(
            exit_code=None if timed_out else proc.returncode,
            stdout=out.text(),
            stderr=err.text(),
            timed_out=timed_out,
            duration_s=time.monotonic() - start,
        )

    def remove_container(self, name: str) -> None:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [self.binary, "rm", "--force", "--volumes", name],
                capture_output=True,
                timeout=120,
                env=self._env(),
                stdin=subprocess.DEVNULL,
            )

    def list_containers(self, label: str) -> list[ContainerInfo]:
        fmt = '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Label "apifix.workspace"}}'
        try:
            proc = subprocess.run(
                [self.binary, "ps", "--all", "--no-trunc", "--filter", f"label={label}"]
                + ["--format", fmt],
                capture_output=True,
                text=True,
                timeout=60,
                env=self._env(),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerError(f"docker ps failed: {exc}") from exc
        if proc.returncode != 0:
            raise DockerError(f"docker ps failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return parse_ps_output(proc.stdout)


def parse_ps_output(text: str) -> list[ContainerInfo]:
    containers = []
    for line in text.splitlines():
        fields = line.split("\t", 3)
        if len(fields) < 3 or not fields[0]:
            continue
        workspace = fields[3] if len(fields) == 4 else ""
        if workspace == "<no value>":  # older docker versions print this for a missing label
            workspace = ""
        containers.append(ContainerInfo(fields[0], fields[1], fields[2], workspace))
    return containers
