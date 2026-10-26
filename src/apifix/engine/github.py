"""GitHub for `apifix pr --push`: fork, push and open the pull request through the `gh` CLI.

apifix never reads a token. `gh` keeps it (its keyring, or `GH_TOKEN` in the environment) and
hands it to `git push` through `gh auth git-credential`; the token is never in an argument or a
remote URL. Everything gh or git prints goes through `scrub` before it is shown or stored, so a
token that leaks into an error message still does not reach the terminal, pr.json or a log.

`GitHub` is the only place that runs `gh`, so a REST or GitHub App backend can replace it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from apifix.engine.gitops import GitError, git, git_text
from apifix.engine.pull_request import update_summary

HOST = "github.com"
GH_TIMEOUT_S = 120
PUSH_TIMEOUT_S = 300
FORK_POLLS = 15
FORK_POLL_S = 2.0
REDACTED = "[token removed]"

_SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# Settings that would point gh somewhere else than github.com / the repo we name.
_GH_DROP = {"GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_DEBUG"}
_GH_FIXED = {
    "GH_HOST": HOST,
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_SPINNER_DISABLED": "1",
    "NO_COLOR": "1",
}
_SECRET_PATTERNS = (
    re.compile(r"\bgh[opsur]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?i)\b(?:proxy-)?authorization\s*:[^\r\n]*"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)x-access-token:[^@\s/]+@?"),
    re.compile(r"(?<=://)[^/\s@:]+:[^/\s@]+@"),  # user:password@host
)
_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_PR_URL = re.compile(rf"https://{re.escape(HOST)}/[\w.-]+/[\w.-]+/pull/\d+")
_ORIGIN = re.compile(
    rf"^(?:https?://(?:[^/@\s]+@)?{re.escape(HOST)}/|git@{re.escape(HOST)}:"
    rf"|ssh://git@{re.escape(HOST)}/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


class GitHubError(Exception):
    """A GitHub step failed or was refused. The message is scrubbed and safe to show."""


def secrets_from_env(env: Mapping[str, str]) -> list[str]:
    """Token values in `env`, longest first (so one containing another is replaced whole)."""
    values = {env[name] for name in _SECRET_ENV if len(env.get(name, "")) >= 6}
    return sorted(values, key=len, reverse=True)


def scrub(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """`text` with anything that looks like a GitHub credential, and the given literal secrets,
    replaced by a placeholder."""
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: REDACTED + ("@" if m.group(0).endswith("@") else ""), text)
    return text


def parse_repo(value: str) -> str:
    """`OWNER/REPO`, or GitHubError."""
    value = value.strip()
    if not _SLUG.fullmatch(value) or value.endswith(".git"):
        raise GitHubError(f"{value!r} is not OWNER/REPO")
    return value


def parse_login(value: str) -> str:
    value = value.strip()
    if not _LOGIN.fullmatch(value):
        raise GitHubError(f"{value!r} is not a GitHub login")
    return value


def repo_from_origin_url(url: str) -> str | None:
    """OWNER/REPO of a github.com remote URL (https, ssh or scp-style), else None."""
    m = _ORIGIN.match(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


@dataclass(frozen=True)
class RepoInfo:
    full_name: str
    default_branch: str
    is_fork: bool
    parent: str | None
    can_push: bool
    private: bool


@dataclass(frozen=True)
class Published:
    user: str
    upstream: str
    fork: str | None  # None when the user owns upstream and the branch went there
    pr_url: str
    draft: bool


class GitHub:
    """`gh` and `git push` for one account on github.com."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        source = os.environ if env is None else env
        self._secrets = secrets_from_env(source)
        self._env = {k: v for k, v in source.items() if k not in _GH_DROP} | _GH_FIXED
        self._sleep = sleep

    def clean(self, text: str) -> str:
        return scrub(text, self._secrets)

    def _gh(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            done = subprocess.run(
                ["gh", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                env=self._env,
                timeout=GH_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            raise GitHubError(
                "`gh` (the GitHub CLI) is needed for --push and is not installed; "
                "see https://cli.github.com"
            ) from None
        except subprocess.TimeoutExpired:
            raise GitHubError(f"gh {args[0]} did not finish in {GH_TIMEOUT_S} s") from None
        done.stdout, done.stderr = self.clean(done.stdout), self.clean(done.stderr)
        if check and done.returncode != 0:
            detail = (done.stderr.strip() or done.stdout.strip())[-600:]
            raise GitHubError(f"gh {' '.join(args[:2])} failed: {detail or done.returncode}")
        return done

    def _json(self, args: list[str]) -> dict:
        out = self._gh(args).stdout
        try:
            value = json.loads(out)
        except ValueError:
            raise GitHubError(f"gh {' '.join(args[:2])} did not return JSON") from None
        if not isinstance(value, dict):
            raise GitHubError(f"gh {' '.join(args[:2])} returned an unexpected answer")
        return value

    def whoami(self) -> str:
        """The login gh acts as on github.com."""
        status = self._gh(["auth", "status", "--hostname", HOST], check=False)
        if status.returncode != 0:
            raise GitHubError(f"gh is not logged in to {HOST}; run `gh auth login`")
        return parse_login(self._gh(["api", "user", "--jq", ".login"]).stdout)

    def repo_info(self, repo: str) -> RepoInfo:
        data = self._json(["api", f"repos/{parse_repo(repo)}"])
        parent = data.get("parent")
        perms = data.get("permissions")
        try:
            return RepoInfo(
                full_name=str(data["full_name"]),
                default_branch=str(data["default_branch"]),
                is_fork=bool(data.get("fork")),
                parent=str(parent["full_name"]) if isinstance(parent, dict) else None,
                can_push=bool(perms.get("push")) if isinstance(perms, dict) else False,
                private=bool(data.get("private")),
            )
        except KeyError as exc:
            raise GitHubError(f"GitHub's answer for {repo} lacks {exc}") from None

    def base_status(self, repo: str, default_branch: str, sha: str) -> str | None:
        """How `sha` relates to the repo's default branch: "identical" or "behind" (an ancestor of
        it), "ahead" or "diverged" (it has commits the branch lacks), None if GitHub has no such
        commit."""
        ref = quote(default_branch, safe="/")
        done = self._gh(
            ["api", f"repos/{parse_repo(repo)}/compare/{ref}...{sha}", "--jq", ".status"],
            check=False,
        )
        if done.returncode == 0:
            return done.stdout.strip() or None
        if re.search(r"HTTP (404|422)", done.stderr):
            return None
        raise GitHubError(f"gh api compare failed: {done.stderr.strip()[-600:]}")

    def ensure_fork(self, upstream: str, user: str) -> str:
        """The user's fork of `upstream` (made if needed). Returns its OWNER/REPO. Refuses a repo
        of that name that is not a fork of upstream."""
        name = f"{user}/{upstream.split('/', 1)[1]}"
        try:
            self._gh(["repo", "fork", upstream, "--clone=false", "--remote=false"])
            failure = None
        except GitHubError as exc:
            failure = exc  # it may exist already; the check below decides
        info = None
        for attempt in range(FORK_POLLS):
            try:
                info = self.repo_info(name)
                break
            except GitHubError:
                if attempt == FORK_POLLS - 1:
                    raise failure or GitHubError(f"the fork {name} did not appear") from None
                self._sleep(FORK_POLL_S)
        assert info is not None
        if not info.is_fork or (info.parent or "").lower() != upstream.lower():
            raise GitHubError(
                f"{name} exists but is not a fork of {upstream}; rename or delete it, or push "
                "by hand"
            )
        return info.full_name

    def find_open_pr(self, upstream: str, user: str, branch: str) -> str | None:
        """URL of an open PR from `user:branch` into upstream, if there is one."""
        query = urlencode({"head": f"{user}:{branch}", "state": "open"})
        out = self._gh(
            [
                "api",
                "-X",
                "GET",
                f"repos/{parse_repo(upstream)}/pulls?{query}",
                "--jq",
                ".[0].html_url // empty",
            ]
        ).stdout.strip()
        return out or None

    def push(self, repo: Path, target: str, branch: str) -> None:
        """Push `branch` to `target` (OWNER/REPO) over https, credentials from gh, never forced.
        No remote is added to the checkout."""
        url = f"https://{HOST}/{parse_repo(target)}.git"
        try:
            git(
                repo,
                ["push", url, f"refs/heads/{branch}:refs/heads/{branch}"],
                config=["credential.helper=", "credential.helper=!gh auth git-credential"],
                env=_GH_FIXED,
                timeout=PUSH_TIMEOUT_S,
            )
        except GitError as exc:
            raise GitHubError(self.clean(str(exc))) from None

    def open_pr(
        self,
        upstream: str,
        head: str,
        base: str,
        title: str,
        body_file: Path,
        *,
        draft: bool,
    ) -> str:
        """Open the PR; returns its URL."""
        args = [
            "pr",
            "create",
            "--repo",
            parse_repo(upstream),
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body-file",
            str(body_file),
        ]
        if draft:
            args.append("--draft")
        try:
            out = self._gh(args).stdout
        except GitHubError as exc:
            if "draft pull requests are not supported" in str(exc).lower():
                raise GitHubError(
                    f"{upstream} does not allow draft pull requests (GitHub Free allows them "
                    f"only in public repositories); run again with --ready. {exc}"
                ) from None
            raise
        url = next(
            (ln.strip() for ln in reversed(out.splitlines()) if _PR_URL.fullmatch(ln.strip())), ""
        )
        if not url:
            raise GitHubError("gh did not print the new pull request's URL")
        return url


class PullRequestPublisher:
    """The `--push` flow around one `GitHub`: `preflight` before the local branch is made,
    `publish` after."""

    def __init__(
        self,
        gh: GitHub,
        repo: Path,
        *,
        expected_user: str,
        upstream: str | None = None,
        ready: bool = False,
    ) -> None:
        self.gh = gh
        self.repo = repo
        self.expected_user = parse_login(expected_user)
        self.upstream_flag = parse_repo(upstream) if upstream else None
        self.draft = not ready
        self.user = ""
        self.upstream = ""
        self.info: RepoInfo | None = None

    def _origin_repo(self) -> str:
        try:
            url = git_text(self.repo, ["config", "--get", "remote.origin.url"])
        except GitError:
            url = ""
        found = repo_from_origin_url(url) if url else None
        if not found:
            raise GitHubError(
                "cannot tell which GitHub repository to open the pull request in (the checkout "
                f"has no {HOST} `origin`); pass --upstream OWNER/REPO"
            )
        return found

    def preflight(self, base: str, branch: str) -> None:
        """Every check that can refuse before anything is created. Raises GitHubError."""
        user = self.gh.whoami()
        if user.lower() != self.expected_user.lower():
            raise GitHubError(
                f"gh is logged in as {user}, but {self.expected_user} was expected "
                "(--github-user / APIFIX_GITHUB_USER); switch with `gh auth switch` or "
                "`gh auth login`, or correct the expected login"
            )
        self.user = user
        self.upstream = self.upstream_flag or self._origin_repo()
        info = self.gh.repo_info(self.upstream)
        self.upstream = info.full_name
        self.info = info
        own = self.upstream.split("/")[0].lower() == user.lower()
        if own and not info.can_push:
            raise GitHubError(f"{user} cannot push to {self.upstream}")
        status = self.gh.base_status(self.upstream, info.default_branch, base)
        if status is None:
            raise GitHubError(
                f"the commit the fix was made on ({base[:12]}) is not on GitHub in "
                f"{self.upstream}; push it first, then run again"
            )
        if status in ("ahead", "diverged"):
            raise GitHubError(
                f"the commit the fix was made on ({base[:12]}) is not in the history of "
                f"{self.upstream}'s {info.default_branch}, so the pull request would carry "
                "other commits; use a checkout of that branch"
            )
        existing = self.gh.find_open_pr(self.upstream, user, branch)
        if existing:
            raise GitHubError(f"a pull request from {user}:{branch} is already open: {existing}")

    def publish(self, run_dir: Path, branch: str) -> Published:
        """Fork if needed, push the branch, open the PR. pr.json records each step as it
        happens. A failure says what was done; running the same command again continues."""
        assert self.info is not None, "preflight first"
        user, upstream = self.user, self.upstream
        title = (run_dir / "pr-title.txt").read_text(encoding="utf-8").strip()
        if not title or "\n" in title:
            raise GitHubError("pr-title.txt must hold one non-empty line")
        update_summary(run_dir, {"github_user": user, "upstream": upstream, "draft": self.draft})
        retry = "; run the same command again to continue"
        own = upstream.split("/")[0].lower() == user.lower()
        try:
            fork = None if own else self.gh.ensure_fork(upstream, user)
            update_summary(run_dir, {"fork": fork})
        except GitHubError as exc:
            raise GitHubError(f"{exc}\nnothing was pushed{retry}") from None
        target = fork or upstream
        try:
            self.gh.push(self.repo, target, branch)
        except GitHubError as exc:
            raise GitHubError(f"{exc}\nnothing was opened{retry}") from None
        update_summary(run_dir, {"pushed": True})
        try:
            url = self.gh.open_pr(
                upstream,
                f"{user}:{branch}",
                self.info.default_branch,
                title,
                run_dir / "pr-body.md",
                draft=self.draft,
            )
        except GitHubError as exc:
            raise GitHubError(
                f"{exc}\nthe branch {branch} is pushed to {target}, but no pull request was "
                f"opened{retry}"
            ) from None
        update_summary(run_dir, {"pr_url": url})
        return Published(user, upstream, fork, url, self.draft)
