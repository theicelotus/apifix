"""Copy tests/fixtures/pr_sandbox to a new directory and make it a git repository with one
commit, for the end-to-end test of `apifix pr --push` (docs/pr-sandbox.md).

    .venv/bin/python scripts/prepare_pr_sandbox.py [DEST]        (default ~/apifix-pr-sandbox)

The commit's author and committer are APIFIX_GIT_NAME / APIFIX_GIT_EMAIL (a GitHub noreply
address), never your git config. Local only: no remote is added, nothing is pushed, GitHub is
not contacted. Refuses a destination that already exists.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from apifix.engine.gitops import GitError, git
from apifix.engine.pull_request import PullRequestError, identity_from_env

SOURCE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "pr_sandbox"
DEFAULT_DEST = Path.home() / "apifix-pr-sandbox"
MESSAGE = "Add a small support bot on the openai 0.28 API"


def prepare(dest: Path, env: dict[str, str]) -> str:
    """Create `dest` as a git repo (branch main, one commit); returns the commit sha."""
    who = identity_from_env(env)
    if dest.exists():
        raise PullRequestError(f"{dest} already exists; remove it or pick another destination")
    shutil.copytree(
        SOURCE, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
    )
    git(dest, ["init", "-q", "-b", "main"])
    git(dest, ["add", "-A"])
    git(dest, ["commit", "-q", "-m", MESSAGE], identity=who)
    author = git(dest, ["log", "-1", "--format=%an <%ae> / %cn <%ce>"]).stdout.decode().strip()
    expected = f"{who.name} <{who.email}> / {who.name} <{who.email}>"
    if author != expected:
        raise PullRequestError(f"the commit was made as {author!r}, not {expected!r}")
    return git(dest, ["rev-parse", "HEAD"]).stdout.decode().strip()


def main(argv: list[str]) -> int:
    dest = Path(argv[0]).expanduser() if argv else DEFAULT_DEST
    try:
        sha = prepare(dest.resolve(), dict(os.environ))
    except (PullRequestError, GitError) as exc:
        print(f"prepare_pr_sandbox: {exc}", file=sys.stderr)
        return 2
    shown = (
        f"~/{dest.resolve().relative_to(Path.home())}"
        if dest.resolve().is_relative_to(Path.home())
        else str(dest)
    )
    print(f"{shown}: one commit {sha[:12]} on main, no remote")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
