"""Git calls that ignore the machine's identity, hooks and global config.

Used where apifix reads a repo's commit (`repo_state`) and where it builds a commit
(pull_request.py). A call gets no global or system config, no hooks and no signing; the repo's
own `.git/config` still applies (it is part of the checkout).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 60
# Variables that point git somewhere else than the directory it is run in.
_REDIRECT = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
}
_IDENTITY = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_COMMITTER_DATE",
    "EMAIL",
)
# Debug output that can print request headers (and with them a token).
_TRACE = {"GIT_TRACE", "GIT_TRACE2", "GIT_TRACE_CURL", "GIT_TRACE_PACKET", "GIT_CURL_VERBOSE"}


class GitError(Exception):
    """A git call failed or git is not usable. The message is safe to show."""


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


def isolated_env(
    identity: Identity | None = None, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment of an isolated git call: the caller's, minus anything that redirects git
    or sets an identity, plus no global/system config. `identity` is author and committer."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _REDIRECT and k not in _IDENTITY and k not in _TRACE
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    if identity:
        env.update(
            GIT_AUTHOR_NAME=identity.name,
            GIT_AUTHOR_EMAIL=identity.email,
            GIT_COMMITTER_NAME=identity.name,
            GIT_COMMITTER_EMAIL=identity.email,
        )
    env.update(extra or {})
    return env


def git(
    repo: str | Path,
    args: Sequence[str],
    *,
    identity: Identity | None = None,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
    check: bool = True,
    config: Sequence[str] = (),
    timeout: float = GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess[bytes]:
    """Run `git -C repo <args>` with hooks, signing and global config off. `config` is extra
    `key=value` settings for this call (`-c`). With `check`, a non-zero exit raises GitError
    carrying git's last stderr lines."""
    cmd = [
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "core.fsmonitor=false",
        *(part for setting in config for part in ("-c", setting)),
        "-C",
        str(repo),
        *args,
    ]
    try:
        done = subprocess.run(
            cmd,
            input=input,
            capture_output=True,
            env=isolated_env(identity, env),
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise GitError("`git` is needed and it is not installed") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} did not finish in {timeout:g} s") from None
    if check and done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[-1000:]
        raise GitError(f"git {args[0]} failed: {detail or f'exit {done.returncode}'}")
    return done


def git_text(repo: str | Path, args: Sequence[str], **kwargs) -> str:
    """Stdout of an isolated git call, stripped."""
    return git(repo, args, **kwargs).stdout.decode("utf-8", "replace").strip()


def repo_state(path: str | Path) -> tuple[str | None, bool | None]:
    """`(HEAD commit, has uncommitted changes)` of the repo whose top-level directory is `path`;
    `(None, None)` if `path` is not a git repository's top level (a subdirectory's diff would
    not apply at the repository root), has no commit, or git fails."""
    root = Path(path).resolve()
    try:
        top = git_text(root, ["rev-parse", "--show-toplevel"])
        if Path(top).resolve() != root:
            return None, None
        commit = git_text(root, ["rev-parse", "--verify", "HEAD^{commit}"])
        status = git(root, ["status", "--porcelain", "--untracked-files=all"]).stdout
    except GitError:
        return None, None
    return commit, bool(status.strip())
