"""scripts/prepare_pr_sandbox.py: the local half of the end-to-end test of `apifix pr --push`
(a git repo with one commit, as the noreply identity). Needs the `git` binary; no network."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from apifix.engine.pull_request import PullRequestError
from apifix.engine.scanner import scan

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "prepare_pr_sandbox", ROOT / "scripts" / "prepare_pr_sandbox.py"
)
prepare_pr_sandbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_pr_sandbox)

ENV = {
    "APIFIX_GIT_NAME": "Pat Maintainer",
    "APIFIX_GIT_EMAIL": "1234+pat@users.noreply.github.com",
}


def git_out(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env, check=True)
    return done.stdout.decode().strip()


def test_makes_a_repo_with_one_commit_as_the_noreply_identity(tmp_path, monkeypatch):
    # a machine identity that must not be used
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Machine\n\temail = private@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("HOME", str(tmp_path))
    dest = tmp_path / "sandbox"
    sha = prepare_pr_sandbox.prepare(dest, {**os.environ, **ENV})
    assert git_out(dest, "rev-parse", "HEAD") == sha
    assert git_out(dest, "rev-list", "--count", "HEAD") == "1"
    assert git_out(dest, "branch", "--show-current") == "main"
    assert git_out(dest, "log", "-1", "--format=%an <%ae> | %cn <%ce>") == (
        "Pat Maintainer <1234+pat@users.noreply.github.com> | "
        "Pat Maintainer <1234+pat@users.noreply.github.com>"
    )
    assert git_out(dest, "remote") == "" and git_out(dest, "status", "--porcelain") == ""
    assert "private@example.com" not in git_out(dest, "log", "--format=%ae%ce")


def test_the_project_is_an_openai_0x_project_with_tests(tmp_path):
    dest = tmp_path / "sandbox"
    prepare_pr_sandbox.prepare(dest, {**os.environ, **ENV})
    assert (dest / "requirements.txt").read_text().startswith("openai==0.28")
    assert sorted(p.name for p in (dest / "tests").glob("test_*.py")) == [
        "test_chat.py",
        "test_search.py",
    ]
    result = scan(dest, "openai-v1")
    assert result.parse_errors == [] and len(result.findings) > 10
    assert not [p for p in dest.rglob("*") if "__pycache__" in p.parts]


def test_refuses_an_existing_destination_and_a_private_email(tmp_path):
    dest = tmp_path / "sandbox"
    dest.mkdir()
    with pytest.raises(PullRequestError, match="already exists"):
        prepare_pr_sandbox.prepare(dest, {**os.environ, **ENV})
    with pytest.raises(PullRequestError, match="noreply"):
        prepare_pr_sandbox.prepare(
            tmp_path / "other", {**os.environ, **ENV, "APIFIX_GIT_EMAIL": "me@example.com"}
        )
    assert not (tmp_path / "other").exists()


def test_main_reports_a_refusal_without_a_traceback(tmp_path, capsys):
    (tmp_path / "x").mkdir()
    assert prepare_pr_sandbox.main([str(tmp_path / "x")]) == 2
    assert "already exists" in capsys.readouterr().err
