"""`apifix pr`, dry run: a local branch with one commit holding a verified run's fix. Real git
repos in a temp dir and a real verify_diff run with a fake sandbox (no Docker, no LLM, no
network). Needs the `git` binary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from apifix import cli
from apifix.engine.diffing import unified_diff
from apifix.engine.gitops import repo_state
from apifix.engine.pull_request import (
    PullRequestError,
    identity_from_env,
    make_pull_request,
    repo_url_from_env,
)
from apifix.engine.verify_diff import verify_diff
from tests.test_verifier import (
    GOOD_FIX,
    ORIGINAL,
    PASSING,
    T_ASK,
    T_STRIP,
    FakeSandbox,
    run,
    write_tree,
)

ENV = {
    "APIFIX_GIT_NAME": "Pat Maintainer",
    "APIFIX_GIT_EMAIL": "1234+pat@users.noreply.github.com",
}
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Setup",
    "GIT_AUTHOR_EMAIL": "setup@example.invalid",
    "GIT_COMMITTER_NAME": "Setup",
    "GIT_COMMITTER_EMAIL": "setup@example.invalid",
}


def sh(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env={**os.environ, **_GIT_ENV},
        check=True,
    )
    return done.stdout.decode().strip()


def make_repo(root: Path, files: dict[str, str | bytes]) -> Path:
    root.mkdir(parents=True)
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(content if isinstance(content, bytes) else content.encode())
    sh(root, "init", "-q", "-b", "main")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "base")
    return root


def make_run(
    tmp: Path,
    original_files=ORIGINAL,
    fixed_files=GOOD_FIX,
    *,
    sandbox=None,
    name: str = "repo",
) -> tuple[Path, Path]:
    """(git checkout, run dir) as `apifix verify` would leave them."""
    repo = make_repo(tmp / name, {k: v for k, v in original_files.items()})
    fixed = write_tree(tmp / f"{name}-fixed", fixed_files)
    diff = tmp / f"{name}-fix.diff"
    diff.write_text(unified_diff(repo, fixed), newline="")
    result = verify_diff(
        repo, "openai-v1", diff, sandbox=sandbox or FakeSandbox(PASSING, [PASSING])
    )
    out = tmp / f"{name}-run"
    out.mkdir()
    cli._write_outputs(
        out, result.diff, result.report_markdown, result.verification.to_dict(), result.trajectory
    )
    return repo, out


@pytest.fixture
def made(tmp_path):
    return make_run(tmp_path)


def edit_json(path: Path, change) -> None:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def refusal(run_dir, repo, env=ENV, **kw) -> str:
    with pytest.raises(PullRequestError) as exc:
        make_pull_request(run_dir, repo, env=env, **kw)
    return str(exc.value)


def tree_state(repo: Path) -> tuple:
    return (
        sh(repo, "rev-parse", "HEAD"),
        sh(repo, "symbolic-ref", "HEAD"),
        sh(repo, "status", "--porcelain=v1", "--untracked-files=all"),
        sh(repo, "ls-files", "-s"),
        sh(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"),
    )


# --- the branch -----------------------------------------------------------------------------


def test_makes_one_commit_with_the_fix_and_leaves_the_checkout_alone(made):
    repo, out = made
    before = tree_state(repo)
    files_before = {p: (repo / p).read_bytes() for p in ORIGINAL}
    result = make_pull_request(out, repo, env=ENV)
    assert result.branch == "apifix/openai-v1"
    assert result.verdict == "verified"
    assert result.files == ["app.py", "requirements.txt", "tests/test_app.py"]
    assert sh(repo, "rev-parse", result.branch) == result.commit
    assert sh(repo, "rev-parse", f"{result.commit}^") == before[0]  # one commit on the base
    assert sh(repo, "rev-list", "--count", f"{before[0]}..{result.commit}") == "1"
    for path, text in GOOD_FIX.items():  # tree == base + diff
        assert sh(repo, "show", f"{result.commit}:{path}") == text.strip()
    # the checkout's working tree, index, HEAD and branches are as they were (plus the new one)
    after = tree_state(repo)
    assert after[:4] == before[:4]
    assert after[4].split() == sorted([*before[4].split(), "apifix/openai-v1"])
    assert {p: (repo / p).read_bytes() for p in ORIGINAL} == files_before
    assert sorted(p.name for p in out.iterdir() if p.name.startswith("pr")) == [
        "pr-body.md",
        "pr-title.txt",
        "pr.json",
    ]
    info = json.loads((out / "pr.json").read_text())
    assert info["commit"] == result.commit and info["pushed"] is False and info["pr_url"] is None
    assert (out / "pr-title.txt").read_text() == "Migrate to the openai 1.x API\n"


def test_author_and_committer_are_the_env_identity_not_the_machines(made, tmp_path, monkeypatch):
    repo, out = made
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = Private Me\n\temail = me@private.example\n")
    sh(repo, "config", "--local", "core.hooksPath", str(tmp_path / "hooks"))
    hook = tmp_path / "hooks" / "reference-transaction"
    hook.parent.mkdir()
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path / 'hook-ran'}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "leak@example.invalid")
    monkeypatch.setenv("EMAIL", "leak@example.invalid")
    result = make_pull_request(out, repo, env=ENV)
    fields = sh(repo, "log", "-1", "--format=%an|%ae|%cn|%ce", result.commit)
    who = "Pat Maintainer|1234+pat@users.noreply.github.com"
    assert fields == f"{who}|{who}"
    assert "private.example" not in sh(repo, "cat-file", "-p", result.commit)
    assert not (tmp_path / "hook-ran").exists()


def test_the_commit_message_names_the_model_verdict_and_apifix(made):
    repo, out = made
    result = make_pull_request(out, repo, env=ENV)
    message = sh(repo, "log", "-1", "--format=%B", result.commit)
    assert message.startswith("Migrate to the openai 1.x API\n")
    assert "Verdict: verified" in message and "Verified by: apifix " in message
    assert "Written by: not recorded" in message  # a verify run: no model


def test_crlf_files_are_committed_byte_for_byte(tmp_path):
    crlf = lambda text: text.replace("\n", "\r\n")  # noqa: E731
    repo, out = make_run(
        tmp_path,
        {**ORIGINAL, "app.py": crlf(textwrap.dedent(ORIGINAL["app.py"]))},
        {**GOOD_FIX, "app.py": crlf(textwrap.dedent(GOOD_FIX["app.py"]))},
    )
    assert b"\r\n" in (out / "fix.diff").read_bytes()
    sh(repo, "config", "core.autocrlf", "true")  # a local setting must not rewrite the blob
    result = make_pull_request(out, repo, env=ENV)
    blob = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{result.commit}:app.py"],
        capture_output=True,
        check=True,
    ).stdout
    assert blob == crlf(textwrap.dedent(GOOD_FIX["app.py"])).encode()


# --- refusals: each check, one reason, nothing created --------------------------------------


def assert_nothing_made(repo: Path, out: Path, before: tuple) -> None:
    assert tree_state(repo) == before
    assert not any((out / n).exists() for n in ("pr.json", "pr-body.md", "pr-title.txt"))


def test_refuses_a_run_dir_without_all_four_files(made):
    repo, out = made
    (out / "report.md").unlink()
    assert "missing report.md" in refusal(out, repo)


def test_refuses_a_rejected_verdict_and_names_the_blocking_reasons(made):
    repo, out = made

    def reject(data):
        data["verdict"] = "rejected"
        data["reasons"] = [{"code": "newly_failing", "severity": "reject", "message": "x"}]

    edit_json(out / "verification.json", reject)
    edit_json(out / "trajectory.json", lambda t: t.update(outcome="rejected"))
    before = tree_state(repo)
    assert refusal(out, repo) == (
        "verdict is rejected: newly_failing (a pull request needs verified or verified_weak)"
    )
    assert_nothing_made(repo, out, before)


def test_refuses_when_verdict_and_outcome_disagree(made):
    repo, out = made
    edit_json(out / "trajectory.json", lambda t: t.update(outcome="unverified"))
    assert "verdict is" in refusal(out, repo)


def test_refuses_a_diff_edited_after_verification(made):
    repo, out = made
    (out / "fix.diff").write_text((out / "fix.diff").read_text() + "\n")
    before = tree_state(repo)
    assert "changed after it was verified" in refusal(out, repo)
    assert_nothing_made(repo, out, before)


def test_refuses_a_run_without_repo_commit_and_says_to_run_verify(made):
    repo, out = made
    edit_json(out / "trajectory.json", lambda t: t["job"].pop("repo_commit"))
    message = refusal(out, repo)
    assert "did not record the repo's commit" in message
    assert "apifix verify" in message and "no LLM needed" in message


def test_refuses_a_run_without_a_diff_digest_and_says_to_run_verify(made):
    repo, out = made
    edit_json(out / "trajectory.json", lambda t: t["job"].pop("diff_sha256"))
    assert "apifix verify" in refusal(out, repo)


def test_refuses_a_run_made_from_a_dirty_tree(made):
    repo, out = made
    edit_json(out / "trajectory.json", lambda t: t["job"].update(repo_dirty=True))
    assert "uncommitted changes" in refusal(out, repo)


def test_refuses_a_checkout_with_uncommitted_changes(made):
    repo, out = made
    (repo / "scratch.txt").write_text("x")
    before = tree_state(repo)
    assert "uncommitted changes" in refusal(out, repo)
    assert tree_state(repo) == before


def test_refuses_a_checkout_that_lacks_the_runs_commit(made, tmp_path):
    _, out = made
    other = make_repo(tmp_path / "other", {"a.txt": "different history"})
    assert "is not in" in refusal(out, other)


def test_refuses_a_directory_that_is_not_a_git_checkout(made, tmp_path):
    _, out = made
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a git checkout" in refusal(out, plain)


def test_refuses_a_subdirectory_of_a_checkout(made):
    repo, out = made
    assert "top-level" in refusal(out, repo / "tests")


@pytest.mark.parametrize(
    "env, reason",
    [
        ({}, "set APIFIX_GIT_NAME and APIFIX_GIT_EMAIL"),
        ({"APIFIX_GIT_NAME": "A"}, "set APIFIX_GIT_NAME and APIFIX_GIT_EMAIL"),
        (
            {"APIFIX_GIT_NAME": "A", "APIFIX_GIT_EMAIL": "me@example.com"},
            "must be a GitHub noreply address",
        ),
        (
            {"APIFIX_GIT_NAME": "A", "APIFIX_GIT_EMAIL": "@users.noreply.github.com"},
            "must be a GitHub noreply address",
        ),
        (
            {"APIFIX_GIT_NAME": "A", "APIFIX_GIT_EMAIL": "x@evil.test @users.noreply.github.com"},
            "must be a GitHub noreply address",
        ),
        (
            {"APIFIX_GIT_NAME": "A<b>", "APIFIX_GIT_EMAIL": "1+a@users.noreply.github.com"},
            "plain name",
        ),
    ],
)
def test_refuses_without_a_noreply_identity_and_never_reads_git_config(made, env, reason):
    repo, out = made
    sh(repo, "config", "--local", "user.name", "Local")
    sh(repo, "config", "--local", "user.email", "local@users.noreply.github.com")
    before = tree_state(repo)
    assert reason in refusal(out, repo, env=env)
    assert_nothing_made(repo, out, before)


def test_noreply_domain_is_matched_case_insensitively():
    who = identity_from_env(
        {"APIFIX_GIT_NAME": "A", "APIFIX_GIT_EMAIL": "1+a@Users.Noreply.GitHub.com"}
    )
    assert who.name == "A"


def test_refuses_to_overwrite_an_existing_branch(made):
    repo, out = made
    sh(repo, "branch", "apifix/openai-v1")
    tip = sh(repo, "rev-parse", "apifix/openai-v1")
    message = refusal(out, repo)
    assert "already exists" in message and "git branch -D apifix/openai-v1" in message
    assert sh(repo, "rev-parse", "apifix/openai-v1") == tip


def test_refuses_a_diff_that_does_not_apply_to_the_runs_commit(made):
    repo, out = made
    diff = (out / "fix.diff").read_bytes().replace(b"openai==0.28.1", b"openai==0.27.0")
    (out / "fix.diff").write_bytes(diff)
    edit_json(
        out / "trajectory.json",
        lambda t: t["job"].update(diff_sha256=hashlib.sha256(diff).hexdigest()),
    )
    before = tree_state(repo)
    assert "does not apply" in refusal(out, repo)
    assert_nothing_made(repo, out, before)


def test_refuses_a_diff_that_creates_a_symlink(made):
    repo, out = made
    diff = (
        (out / "fix.diff").read_bytes()
        + b"diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n"
        + b"@@ -0,0 +1 @@\n+/etc/passwd\n"
        + b"\\ No newline at end of file\n"
    )
    (out / "fix.diff").write_bytes(diff)
    edit_json(
        out / "trajectory.json",
        lambda t: t["job"].update(diff_sha256=hashlib.sha256(diff).hexdigest()),
    )
    assert "symlink" in refusal(out, repo)
    assert "apifix/openai-v1" not in sh(repo, "branch", "--list")


# --- the text -------------------------------------------------------------------------------


def test_a_dirty_apifix_build_is_allowed_and_the_body_says_so(made):
    repo, out = made
    edit_json(
        out / "trajectory.json",
        lambda t: t["job"]["apifix"].update(commit="a" * 40, dirty=True),
    )
    result = make_pull_request(out, repo, env=ENV)
    assert result.dirty_apifix is True
    assert "uncommitted changes, so the commit does not exactly" in (out / "pr-body.md").read_text()
    assert json.loads((out / "pr.json").read_text())["apifix_dirty"] is True


def test_the_body_has_no_apifix_link_without_a_repo_url_and_one_with_it(made):
    repo, out = made
    make_pull_request(out, repo, env=ENV)
    plain = (out / "pr-body.md").read_text()
    assert "Re-check" not in plain and "apifix verify" not in plain
    assert plain.rstrip().endswith("Feel free to close this if it isn't useful.")

    sh(repo, "branch", "-D", "apifix/openai-v1")
    env = {**ENV, "APIFIX_REPO_URL": "https://example.test/apifix"}
    make_pull_request(out, repo, env=env)
    linked = (out / "pr-body.md").read_text()
    assert "[apifix](https://example.test/apifix)" in linked
    assert "apifix verify . --migration openai-v1 --diff fix.diff" in linked
    assert linked.rstrip().endswith("Feel free to close this if it isn't useful.")


@pytest.mark.parametrize(
    "url", ["example.test", "javascript:alert(1)", "https://a b", "https://x)[y"]
)
def test_a_bad_repo_url_is_refused(made, url):
    repo, out = made
    assert "APIFIX_REPO_URL" in refusal(out, repo, env={**ENV, "APIFIX_REPO_URL": url})
    assert repo_url_from_env({}) is None


def test_a_lockfile_the_fix_leaves_stale_is_named_with_the_ci_warning(tmp_path):
    original = {**ORIGINAL, "poetry.lock": "# lock\n"}
    original["pyproject.toml"] = '[tool.poetry.dependencies]\nopenai = "0.28.1"\n'
    fixed = {
        **GOOD_FIX,
        "poetry.lock": "# lock\n",
        "pyproject.toml": '[tool.poetry.dependencies]\nopenai = ">=1.55.3,<3"\n',
    }
    repo, out = make_run(tmp_path, original, fixed)
    result = make_pull_request(out, repo, env=ENV)
    assert result.lockfiles == ["poetry.lock"]
    body = (out / "pr-body.md").read_text()
    assert "## Lockfiles" in body
    assert "Not regenerated: `poetry.lock`. The repo's CI may fail until it is regenerated." in body


def test_a_lockfile_the_fix_changes_is_not_reported(tmp_path):
    original = {**ORIGINAL, "poetry.lock": "# lock\n"}
    original["pyproject.toml"] = '[tool.poetry.dependencies]\nopenai = "0.28.1"\n'
    fixed = {
        **GOOD_FIX,
        "poetry.lock": "# new lock\n",
        "pyproject.toml": '[tool.poetry.dependencies]\nopenai = ">=1.55.3,<3"\n',
    }
    repo, out = make_run(tmp_path, original, fixed)
    assert make_pull_request(out, repo, env=ENV).lockfiles == []
    assert "Lockfiles" not in (out / "pr-body.md").read_text()


def test_verified_weak_runs_are_accepted_and_lead_with_what_the_tests_missed(tmp_path):
    weak = run((T_ASK, "passed"), (T_STRIP, "passed"), coverage=None)
    repo, out = make_run(tmp_path, sandbox=FakeSandbox(PASSING, [weak]))
    result = make_pull_request(out, repo, env=ENV)
    assert result.verdict == "verified_weak"
    body = (out / "pr-body.md").read_text()
    assert body.index("What the tests did not cover") < body.index("## Why")


def test_outputs_do_not_contain_the_home_directory(made, monkeypatch):
    repo, out = made
    home = str(Path.home())
    edit_json(out / "trajectory.json", lambda t: t["job"].update(repo=f"{home}/src/repo"))
    make_pull_request(out, repo, env=ENV)
    for name in ("pr-title.txt", "pr-body.md", "pr.json"):
        assert home not in (out / name).read_text()


# --- helpers and the CLI --------------------------------------------------------------------


def test_repo_state(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repo_state(plain) == (None, None)
    repo = make_repo(tmp_path / "r", {"a.py": "x = 1\n"})
    commit, dirty = repo_state(repo)
    assert commit == sh(repo, "rev-parse", "HEAD") and dirty is False
    (repo / "b.py").write_text("y\n")
    assert repo_state(repo) == (commit, True)
    assert repo_state(repo / "sub") == (None, None)  # not a directory
    (repo / "sub").mkdir()
    assert repo_state(repo / "sub") == (None, None)  # not the top level


def test_the_fix_and_verify_trajectories_record_the_commit_and_digest(made):
    repo, out = made
    job = json.loads((out / "trajectory.json").read_text())["job"]
    assert job["repo_commit"] == sh(repo, "rev-parse", "HEAD") and job["repo_dirty"] is False
    assert job["diff_sha256"] == hashlib.sha256((out / "fix.diff").read_bytes()).hexdigest()


def test_cli_exit_codes(made, monkeypatch, capsys):
    repo, out = made
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert cli.main(["pr", str(out), "--repo", str(repo)]) == cli.EXIT_CLEAN
    shown = capsys.readouterr().out
    assert "apifix/openai-v1" in shown and "nothing was pushed" in shown
    # again: the branch exists now
    assert cli.main(["pr", str(out), "--repo", str(repo)]) == cli.EXIT_USAGE
    assert "already exists" in capsys.readouterr().err
    monkeypatch.delenv("APIFIX_GIT_EMAIL")
    sh(repo, "branch", "-D", "apifix/openai-v1")
    assert cli.main(["pr", str(out), "--repo", str(repo)]) == cli.EXIT_USAGE
    assert "APIFIX_GIT_EMAIL" in capsys.readouterr().err


def test_the_summary_line_has_singular_and_plural_forms(made, monkeypatch, capsys):
    repo, out = made
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    assert cli._plural(1, "file") == "1 file" and cli._plural(3, "file") == "3 files"
    assert cli._plural(0, "file") == "0 files"
    assert cli.main(["pr", str(out), "--repo", str(repo)]) == cli.EXIT_CLEAN
    shown = capsys.readouterr().out
    count = len(json.loads((out / "pr.json").read_text())["files"])
    assert f"{cli._plural(count, 'file')} in one commit" in shown and "file(s)" not in shown
