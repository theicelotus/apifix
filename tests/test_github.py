"""`apifix pr --push`: the gh wrapper, the token scrubber and the whole flow. A fake `gh` and
`git` are the only programs on PATH (tests/fake_github.py), so nothing here can reach GitHub or
run a real `git push`. Needs the `git` binary (the fake runs it for everything but a push)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from apifix import cli
from apifix.engine.github import (
    GitHub,
    GitHubError,
    PullRequestPublisher,
    parse_login,
    parse_repo,
    repo_from_origin_url,
    scrub,
)
from apifix.engine.pull_request import make_pull_request
from tests.fake_github import FAKE_TOKEN, install, token_free, tree_text
from tests.test_pull_request import ENV, edit_json, make_run, sh

BRANCH = "apifix/openai-v1"


@pytest.fixture
def made(tmp_path, monkeypatch):
    """(checkout, run dir, fake GitHub): a verified run, then the fakes alone on PATH."""
    repo, out = make_run(tmp_path)
    # The run records the state of this apifix checkout; a dev tree is often dirty.
    edit_json(out / "trajectory.json", lambda t: t["job"]["apifix"].update(dirty=False))
    fake = install(tmp_path, monkeypatch)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    return repo, out, fake


def push_args(out: Path, repo: Path, *extra: str, user: str | None = "theicelotus") -> list[str]:
    args = ["pr", str(out), "--repo", str(repo), "--push", "--upstream", "owner/repo", *extra]
    return args + (["--github-user", user] if user else [])


def branches(repo: Path) -> str:
    return sh(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")


def run_cli(args: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the fakes are the only thing on PATH ---------------------------------------------------


def test_only_the_fakes_are_on_path(made):
    _, _, fake = made
    assert Path(shutil.which("gh")).parent == fake.bin
    assert Path(shutil.which("git")).parent == fake.bin


# --- scrubber and parsers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "gho_abcdefghijklmnopqrstuvwxyz0123456789 and ghs_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz_0123456789",
        "fatal: unable to access 'https://x-access-token:s3cr3tvalue@github.com/o/r.git/'",
        "fatal: unable to access 'https://theicelotus:s3cr3tvalue@github.com/o/r.git/'",
        "> Authorization: Basic c2VjcmV0dmFsdWU6eA==",
        "authorization: token s3cr3tvalue",
        "Bearer s3cr3tvalue12345",
    ],
)
def test_scrub_removes_credentials(text):
    out = scrub(text)
    assert "s3cr3tvalue" not in out and "abcdefghijklmnopqrstuvwxyz" not in out
    assert "c2VjcmV0" not in out and "[token removed]" in out


def test_scrub_keeps_ordinary_text_and_removes_literal_secrets():
    plain = "pushed to https://github.com/o/r.git (user@github.com), branch apifix/openai-v1"
    assert scrub(plain) == plain
    assert scrub("the key is hunter2xx here", ["hunter2xx"]) == "the key is [token removed] here"
    assert scrub("fatal: https://x-access-token:abc@github.com/o/r").endswith("@github.com/o/r")


def test_parsers():
    assert parse_repo("owner/repo.name") == "owner/repo.name"
    for bad in ("owner", "a/b/c", "owner/repo.git", "o w/r", "../x/y", ""):
        with pytest.raises(GitHubError):
            parse_repo(bad)
    assert parse_login(" the-user1 ") == "the-user1"
    for bad in ("", "-x", "a b", "x" * 40, "a/b"):
        with pytest.raises(GitHubError):
            parse_login(bad)
    assert repo_from_origin_url("https://github.com/o/r.git") == "o/r"
    assert repo_from_origin_url("https://github.com/o/r") == "o/r"
    assert repo_from_origin_url("git@github.com:o/r.git") == "o/r"
    assert repo_from_origin_url("ssh://git@github.com/o/r.git") == "o/r"
    assert repo_from_origin_url("https://x-access-token:abc@github.com/o/r.git") == "o/r"
    assert repo_from_origin_url("https://gitlab.com/o/r.git") is None
    assert repo_from_origin_url("https://github.com.evil.example/o/r.git") is None
    assert repo_from_origin_url("/some/path") is None


# --- the GitHub class -----------------------------------------------------------------------


def test_whoami(made):
    _, _, fake = made
    assert GitHub().whoami() == "theicelotus"
    fake.set(logged_in=False)
    with pytest.raises(GitHubError, match="not logged in"):
        GitHub().whoami()


def test_gh_runs_against_github_com_with_prompts_off(made, monkeypatch):
    _, _, fake = made
    monkeypatch.setenv("GH_HOST", "ghe.example.com")
    GitHub().whoami()
    assert {c["gh_host"] for c in fake.calls("gh")} == {"github.com"}


def test_push_uses_https_and_the_gh_credential_helper_and_never_forces(made):
    repo, _, fake = made
    GitHub().push(repo, "theicelotus/repo", BRANCH)
    (push,) = fake.pushes()
    argv = push["argv"]
    assert "https://github.com/theicelotus/repo.git" in argv
    assert f"refs/heads/{BRANCH}:refs/heads/{BRANCH}" in argv
    assert "credential.helper=" in argv and "credential.helper=!gh auth git-credential" in argv
    assert not [a for a in argv if "force" in a or a.startswith("+")]
    assert all(token_free(a) for a in argv)
    assert sh(repo, "remote") == ""  # no remote was added to the checkout


def test_a_failed_push_is_reported_without_the_token(made):
    repo, _, fake = made
    fake.set(
        push_error="fatal: unable to access 'https://x-access-token:{token}@github.com/o/r.git/'"
        ": The requested URL returned error: 403\nAuthorization: token {token}"
    )
    with pytest.raises(GitHubError) as exc:
        GitHub().push(repo, "o/r", BRANCH)
    assert "403" in str(exc.value) and token_free(str(exc.value))


def test_a_gh_failure_is_reported_without_the_token(made):
    _, _, fake = made
    with pytest.raises(GitHubError) as exc:
        GitHub().repo_info("nobody/nothing")
    assert "404" in str(exc.value) and token_free(str(exc.value))
    fake.set(logged_in=False)
    assert token_free(GitHub().clean(f"a {FAKE_TOKEN} b"))


def test_ensure_fork_creates_one_and_reuses_it(made):
    _, _, fake = made
    gh = GitHub(sleep=lambda s: None)
    assert gh.ensure_fork("owner/repo", "theicelotus") == "theicelotus/repo"
    assert gh.ensure_fork("owner/repo", "theicelotus") == "theicelotus/repo"
    assert fake.state["repos"]["theicelotus/repo"]["parent"] == {"full_name": "owner/repo"}


def test_ensure_fork_waits_for_the_fork_to_appear(made):
    _, _, fake = made
    fake.set(view_failures=3)
    slept = []
    assert GitHub(sleep=slept.append).ensure_fork("owner/repo", "theicelotus") == "theicelotus/repo"
    assert len(slept) == 3


def test_ensure_fork_refuses_a_repo_that_is_not_a_fork_of_upstream(made):
    _, _, fake = made
    fake.add_repo("theicelotus/repo")  # same name, not a fork
    with pytest.raises(GitHubError, match="not a fork of owner/repo"):
        GitHub(sleep=lambda s: None).ensure_fork("owner/repo", "theicelotus")


def test_ensure_fork_reports_a_fork_that_never_appears(made):
    _, _, fake = made
    fake.set(fork_error="gh: forking is disabled for this repository", view_failures=0)
    with pytest.raises(GitHubError, match="forking is disabled"):
        GitHub(sleep=lambda s: None).ensure_fork("owner/repo", "theicelotus")


def test_open_pr_is_a_draft_unless_ready_and_returns_the_url(made):
    _, out, fake = made
    body = out / "pr-body.md"
    body.write_text("body\n")
    url = GitHub().open_pr("owner/repo", "me:b", "main", "Title", body, draft=True)
    assert url == "https://github.com/owner/repo/pull/7"
    GitHub().open_pr("owner/repo", "me:b", "main", "Title", body, draft=False)
    first, second = fake.created_prs()
    assert "--draft" in first and "--draft" not in second
    assert first[first.index("--head") + 1] == "me:b"
    assert first[first.index("--body-file") + 1] == str(body)


def test_open_pr_explains_a_repo_without_drafts(made):
    _, out, fake = made
    fake.set(
        pr_error="pull request create failed: Draft pull requests are not supported in this repo"
    )
    with pytest.raises(GitHubError, match="run again with --ready"):
        GitHub().open_pr("owner/repo", "me:b", "main", "T", out / "pr-body.md", draft=True)


def test_open_pr_needs_a_url_in_ghs_answer(made):
    _, out, fake = made
    fake.set(pr_url="not a url")
    with pytest.raises(GitHubError, match="did not print"):
        GitHub().open_pr("owner/repo", "me:b", "main", "T", out / "pr-body.md", draft=True)


# --- the flow through the CLI ---------------------------------------------------------------


def test_without_push_gh_is_never_called_and_nothing_is_pushed(made, capsys):
    repo, out, fake = made
    code, shown, _ = run_cli(["pr", str(out), "--repo", str(repo)], capsys)
    assert code == cli.EXIT_CLEAN and "nothing was pushed" in shown
    assert fake.calls("gh") == [] and fake.pushes() == []


def _kind(argv: list[str]) -> str:
    """A gh call reduced to what it does: its API path, or `fork` / `create` / `auth`."""
    if argv[0] == "auth":
        return "auth"
    if argv[:2] == ["repo", "fork"]:
        return "fork"
    if argv[:2] == ["pr", "create"]:
        return "create"
    path = next(a for a in argv[1:] if a == "user" or a.startswith("repos/"))
    return path.split("?")[0]


def test_push_into_a_fork_runs_the_steps_in_order(made, capsys):
    repo, out, fake = made
    code, shown, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_CLEAN, err
    kinds = [_kind(a) for a in fake.gh_calls()]
    assert kinds[0] == "auth" and kinds[1] == "user"
    assert kinds[2] == "repos/owner/repo"
    assert kinds[3].startswith("repos/owner/repo/compare/main...")
    assert kinds[4] == "repos/owner/repo/pulls"
    assert kinds[5] == "fork" and kinds[6] == "repos/theicelotus/repo" and kinds[-1] == "create"
    # the push came after the fork and before the PR; it went to the fork, never upstream
    (push,) = fake.pushes()
    assert "https://github.com/theicelotus/repo.git" in push["argv"]
    (create,) = fake.created_prs()
    assert create[create.index("--repo") + 1] == "owner/repo"
    assert create[create.index("--head") + 1] == f"theicelotus:{BRANCH}"
    assert create[create.index("--base") + 1] == "main" and "--draft" in create
    title = (out / "pr-title.txt").read_text().strip()
    assert create[create.index("--title") + 1] == title
    assert (
        "theicelotus" in shown
        and "draft pull request: https://github.com/owner/repo/pull/7" in shown
    )
    assert "theicelotus/repo (fork of owner/repo)" in shown


def test_pr_json_records_the_account_and_each_step(made, capsys):
    repo, out, fake = made
    run_cli(push_args(out, repo), capsys)
    summary = json.loads((out / "pr.json").read_text())
    assert summary["github_user"] == "theicelotus" and summary["upstream"] == "owner/repo"
    assert summary["fork"] == "theicelotus/repo" and summary["draft"] is True
    assert summary["pushed"] is True
    assert summary["pr_url"] == "https://github.com/owner/repo/pull/7"
    assert summary["commit"] == sh(repo, "rev-parse", BRANCH)


def test_push_to_a_repo_the_user_owns_skips_the_fork(made, capsys):
    repo, out, fake = made
    fake.add_repo("theicelotus/sandbox")
    fake.set(pr_url="https://github.com/theicelotus/sandbox/pull/1")
    args = push_args(out, repo)
    args[args.index("owner/repo")] = "theicelotus/sandbox"
    code, shown, err = run_cli(args, capsys)
    assert code == cli.EXIT_CLEAN, err
    assert ["repo", "fork"] not in [a[:2] for a in fake.gh_calls()]
    (push,) = fake.pushes()
    assert "https://github.com/theicelotus/sandbox.git" in push["argv"]
    (create,) = fake.created_prs()
    assert create[create.index("--head") + 1] == f"theicelotus:{BRANCH}"
    assert json.loads((out / "pr.json").read_text())["fork"] is None


def test_ready_opens_a_pull_request_that_is_not_a_draft(made, capsys):
    repo, out, fake = made
    code, shown, _ = run_cli(push_args(out, repo, "--ready"), capsys)
    assert code == cli.EXIT_CLEAN and "opened a pull request:" in shown
    (create,) = fake.created_prs()
    assert "--draft" not in create
    assert json.loads((out / "pr.json").read_text())["draft"] is False


def test_the_upstream_comes_from_the_checkouts_origin(made, capsys):
    repo, out, fake = made
    sh(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    args = push_args(out, repo)
    del args[args.index("--upstream") : args.index("--upstream") + 2]
    code, _, err = run_cli(args, capsys)
    assert code == cli.EXIT_CLEAN, err
    assert fake.created_prs()[0][fake.created_prs()[0].index("--repo") + 1] == "owner/repo"


def test_no_upstream_and_no_github_origin_is_refused(made, capsys):
    repo, out, fake = made
    args = push_args(out, repo)
    del args[args.index("--upstream") : args.index("--upstream") + 2]
    code, _, err = run_cli(args, capsys)
    assert code == cli.EXIT_USAGE and "--upstream OWNER/REPO" in err
    assert BRANCH not in branches(repo) and fake.pushes() == []


# --- refusals: nothing is made, nothing is pushed -------------------------------------------


def assert_refused(code, err, repo, fake, reason):
    assert code == cli.EXIT_USAGE and reason in err, err
    assert BRANCH not in branches(repo)
    assert fake.pushes() == [] and fake.created_prs() == []
    assert ["repo", "fork"] not in [a[:2] for a in fake.gh_calls()]


def test_push_needs_the_expected_login(made, capsys):
    repo, out, fake = made
    code, _, err = run_cli(push_args(out, repo, user=None), capsys)
    assert_refused(code, err, repo, fake, "APIFIX_GITHUB_USER")
    assert fake.calls("gh") == []


def test_the_expected_login_can_come_from_the_environment(made, monkeypatch, capsys):
    repo, out, fake = made
    monkeypatch.setenv("APIFIX_GITHUB_USER", "theicelotus")
    code, _, err = run_cli(push_args(out, repo, user=None), capsys)
    assert code == cli.EXIT_CLEAN, err


def test_a_different_logged_in_account_is_refused(made, capsys):
    repo, out, fake = made
    fake.set(login="someone-else")
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert_refused(code, err, repo, fake, "logged in as someone-else, but theicelotus")


def test_the_login_comparison_ignores_case(made, capsys):
    repo, out, fake = made
    code, _, err = run_cli(push_args(out, repo, user="TheIceLotus"), capsys)
    assert code == cli.EXIT_CLEAN, err


def test_gh_not_logged_in_is_refused(made, capsys):
    repo, out, fake = made
    fake.set(logged_in=False)
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert_refused(code, err, repo, fake, "not logged in")
    assert token_free(err)


@pytest.mark.parametrize("flags", [["--ready"], ["--upstream", "o/r"], ["--github-user", "x"]])
def test_push_only_flags_without_push_are_a_usage_error(made, capsys, flags):
    repo, out, fake = made
    code, _, err = run_cli(["pr", str(out), "--repo", str(repo), *flags], capsys)
    assert code == cli.EXIT_USAGE and "only apply with --push" in err
    assert BRANCH not in branches(repo) and fake.calls("gh") == []


def test_a_dirty_apifix_build_is_refused_with_push_and_allowed_without(made, capsys):
    repo, out, fake = made
    edit_json(out / "trajectory.json", lambda t: t["job"]["apifix"].update(dirty=True))
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert_refused(code, err, repo, fake, "dirty apifix build")
    assert "apifix verify" in err and fake.calls("gh") == []
    code, shown, _ = run_cli(["pr", str(out), "--repo", str(repo)], capsys)
    assert code == cli.EXIT_CLEAN and "dirty apifix build" in shown


def test_an_open_pull_request_for_the_branch_is_refused(made, capsys):
    repo, out, fake = made
    url = "https://github.com/owner/repo/pull/3"
    fake.set(open_prs=[{"head": f"theicelotus:{BRANCH}", "url": url}])
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert_refused(code, err, repo, fake, f"already open: {url}")


def test_a_base_commit_missing_from_github_is_refused(made, capsys):
    repo, out, fake = made
    fake.set(compare="missing")
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert_refused(code, err, repo, fake, "is not on GitHub")


def test_a_base_commit_outside_the_default_branch_is_refused(made, capsys):
    repo, out, fake = made
    fake.set(compare="ahead")
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert_refused(code, err, repo, fake, "would carry other commits")


def test_an_upstream_the_user_owns_but_cannot_push_to_is_refused(made, capsys):
    repo, out, fake = made
    fake.add_repo("theicelotus/readonly", push=False)
    args = push_args(out, repo)
    args[args.index("owner/repo")] = "theicelotus/readonly"
    code, _, err = run_cli(args, capsys)
    assert_refused(code, err, repo, fake, "cannot push to theicelotus/readonly")


def test_a_missing_upstream_is_refused_without_the_token_in_the_message(made, capsys):
    repo, out, fake = made
    args = push_args(out, repo)
    args[args.index("owner/repo")] = "nobody/nothing"
    code, _, err = run_cli(args, capsys)
    assert_refused(code, err, repo, fake, "404")
    assert token_free(err)


def test_refusals_of_the_dry_run_still_apply_with_push(made, capsys, monkeypatch):
    repo, out, fake = made
    monkeypatch.delenv("APIFIX_GIT_EMAIL")
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert_refused(code, err, repo, fake, "APIFIX_GIT_EMAIL")
    assert fake.calls("gh") == []


# --- failures after the branch exists, and running again ------------------------------------


def test_a_failed_pr_creation_leaves_a_branch_that_the_same_command_continues(made, capsys):
    repo, out, fake = made
    fake.set(pr_error="pull request create failed: boom")
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_USAGE and "was not opened" in err
    assert f"the branch {BRANCH} is pushed to theicelotus/repo" in err
    first = sh(repo, "rev-parse", BRANCH)
    summary = json.loads((out / "pr.json").read_text())
    assert summary["pushed"] is True and summary["pr_url"] is None
    # edit the body by hand, fix the problem, run again: same commit, the edit is kept
    (out / "pr-body.md").write_text("hand-edited body\n")
    fake.set(pr_error=None)
    code, shown, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_CLEAN, err
    assert "reused the branch of an unfinished push" in shown
    assert sh(repo, "rev-parse", BRANCH) == first
    assert (out / "pr-body.md").read_text() == "hand-edited body\n"
    assert len(fake.pushes()) == 2 and len(fake.created_prs()) == 1
    assert json.loads((out / "pr.json").read_text())["pr_url"].endswith("/pull/7")


def test_a_failed_push_says_nothing_was_opened(made, capsys):
    repo, out, fake = made
    fake.set(push_error="fatal: unable to access 'https://x-access-token:{token}@github.com/o/r'")
    code, shown, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_USAGE and "nothing was opened" in err and fake.created_prs() == []
    assert token_free(err + shown) and token_free(tree_text(out))
    assert json.loads((out / "pr.json").read_text())["pushed"] is False


def test_a_branch_apifix_did_not_make_is_never_reused(made, capsys):
    repo, out, fake = made
    sh(repo, "branch", BRANCH)  # some other commit-less branch at HEAD
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_USAGE and "already exists" in err
    assert fake.pushes() == [] and fake.calls("gh") == []


def test_a_finished_run_is_not_reused(made, capsys):
    repo, out, fake = made
    assert run_cli(push_args(out, repo), capsys)[0] == cli.EXIT_CLEAN
    fake.set(open_prs=[])  # even if the PR were closed: pr.json records a URL
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_USAGE and "already exists" in err


def test_a_branch_reused_after_the_diff_changed_is_refused(made, capsys):
    repo, out, fake = made
    fake.set(pr_error="boom")
    run_cli(push_args(out, repo), capsys)
    fake.set(pr_error=None)
    edit_json(out / "pr.json", lambda s: s.update(commit="0" * 40))
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_USAGE and "already exists" in err


# --- the token never shows -------------------------------------------------------------------


def test_the_token_is_in_no_argument_output_or_written_file(made, capsys):
    repo, out, fake = made
    code, shown, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_CLEAN
    assert token_free(shown + err) and token_free(tree_text(out))
    for call in fake.calls():
        assert all(token_free(a) for a in call["argv"])
    for created in fake.created_prs():
        assert all(token_free(a) for a in created)
    assert all(c["has_token"] for c in fake.calls("gh"))  # gh got it from its environment
    assert FAKE_TOKEN not in sh(repo, "config", "--list")


def test_github_user_is_stored_and_shown(made, capsys):
    repo, out, fake = made
    _, shown, _ = run_cli(push_args(out, repo), capsys)
    assert "GitHub account: theicelotus" in shown
    assert json.loads((out / "pr.json").read_text())["github_user"] == "theicelotus"


# --- the publisher on its own ----------------------------------------------------------------


def test_publish_needs_a_preflight_first(made):
    repo, out, _ = made
    publisher = PullRequestPublisher(GitHub(), repo, expected_user="theicelotus")
    with pytest.raises(AssertionError):
        publisher.publish(out, BRANCH)


def test_make_pull_request_calls_the_preflight_before_creating_anything(made):
    repo, out, _ = made
    seen = []

    def preflight(base, branch):
        seen.append((base, branch, branches(repo)))
        raise GitHubError("stop here")

    with pytest.raises(GitHubError, match="stop here"):
        make_pull_request(out, repo, env=ENV, preflight=preflight)
    base = sh(repo, "rev-parse", "HEAD")
    assert seen == [(base, BRANCH, "main")]
    assert BRANCH not in branches(repo) and not (out / "pr.json").exists()


def test_push_reuses_the_branch_a_dry_run_made(made, capsys):
    repo, out, fake = made
    assert run_cli(["pr", str(out), "--repo", str(repo)], capsys)[0] == cli.EXIT_CLEAN
    first = sh(repo, "rev-parse", BRANCH)
    (out / "pr-title.txt").write_text("A title I edited\n")
    code, shown, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_CLEAN, err
    assert sh(repo, "rev-parse", BRANCH) == first
    (create,) = fake.created_prs()
    assert create[create.index("--title") + 1] == "A title I edited"


def test_a_reused_branch_still_needs_the_checkout_to_be_clean(made, capsys):
    repo, out, fake = made
    run_cli(["pr", str(out), "--repo", str(repo)], capsys)
    (repo / "scratch.txt").write_text("x\n")
    code, _, err = run_cli(push_args(out, repo), capsys)
    assert code == cli.EXIT_USAGE and "uncommitted changes" in err
    assert fake.calls("gh") == [] and fake.pushes() == []
