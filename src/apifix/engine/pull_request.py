"""`apifix pr`, dry run: a local branch with one commit holding a verified fix, and the pull
request text. Nothing is pushed and the checkout's working tree, index and current branch are
not touched.

The commit is built from the run's fix.diff and the commit the run started from
(`job.repo_commit`), using a temporary index, so it is exactly base + diff.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from apifix.engine.dependencies import is_dependency_file
from apifix.engine.gitops import GitError, Identity, git, git_text
from apifix.engine.local_paths import redact_local_paths
from apifix.engine.migrations import get_migration
from apifix.engine.pin_edits import LOCKFILES
from apifix.engine.pr_body import ChangedFile, PullRequestText, build_pull_request_text
from apifix.engine.verify_diff import VerifyError, check_no_symlinks

OK_VERDICTS = ("verified", "verified_weak")
RUN_FILES = ("fix.diff", "report.md", "verification.json", "trajectory.json")
OUTPUT_FILES = ("pr-title.txt", "pr-body.md", "pr.json")
NOREPLY_DOMAIN = "@users.noreply.github.com"
_NOT_CONTROL = re.compile(r"[\x00-\x1f\x7f<>]")
_NOT_RECORDED = re.compile(r"^not updated: (\S+); regenerate it$")


class PullRequestError(Exception):
    """The branch cannot be made; the message is the reason, written for the user."""


@dataclass
class PullRequestResult:
    branch: str
    base: str
    commit: str
    title: str
    files: list[str]
    verdict: str
    dirty_apifix: bool
    lockfiles: list[str]
    outputs: list[str]
    resumed: bool = False  # the branch was made by an earlier `--push` run that did not finish


def identity_from_env(env: Mapping[str, str]) -> Identity:
    """APIFIX_GIT_NAME / APIFIX_GIT_EMAIL. Never git config: the machine's own identity (often
    a private address) must not end up in a public commit."""
    name = env.get("APIFIX_GIT_NAME", "").strip()
    email = env.get("APIFIX_GIT_EMAIL", "").strip()
    if not name or not email:
        raise PullRequestError(
            "set APIFIX_GIT_NAME and APIFIX_GIT_EMAIL: the commit uses that identity, never "
            "your git config"
        )
    local = email[: -len(NOREPLY_DOMAIN)] if email.lower().endswith(NOREPLY_DOMAIN) else ""
    if not local or _NOT_CONTROL.search(email) or " " in email or "@" in local:
        raise PullRequestError(
            f"APIFIX_GIT_EMAIL must be a GitHub noreply address (…{NOREPLY_DOMAIN}), "
            "so a private email is not published in the commit"
        )
    if _NOT_CONTROL.search(name):
        raise PullRequestError("APIFIX_GIT_NAME must be a plain name (no <, > or control chars)")
    return Identity(name, email)


def repo_url_from_env(env: Mapping[str, str]) -> str | None:
    url = env.get("APIFIX_REPO_URL", "").strip()
    if not url:
        return None
    if not re.fullmatch(r"https?://[^\s<>()\[\]`]+", url):
        raise PullRequestError("APIFIX_REPO_URL must be an http(s) URL without spaces")
    return url


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PullRequestError(f"cannot read {path.name}: {exc}") from None
    if not isinstance(value, dict):
        raise PullRequestError(f"{path.name} is not a JSON object")
    return value


def _verify_hint(repo: Path, run_dir: Path, migration: str) -> str:
    return (
        f"re-run `apifix verify {repo} --migration {migration} --diff {run_dir}/fix.diff` "
        "(no LLM needed) from a clean checkout"
    )


def load_run(run_dir: Path, repo: Path) -> tuple[dict, dict, bytes]:
    """The verified run's files, after checks 1-3 (completeness, verdict, diff and commit
    records). Raises PullRequestError with the first reason that applies."""
    missing = [name for name in RUN_FILES if not (run_dir / name).is_file()]
    if missing:
        raise PullRequestError(f"{run_dir} is not a finished run: missing {', '.join(missing)}")
    verification = _load_json(run_dir / "verification.json")
    trajectory = _load_json(run_dir / "trajectory.json")
    diff = (run_dir / "fix.diff").read_bytes()
    verdict, outcome = verification.get("verdict"), trajectory.get("outcome")
    if verdict not in OK_VERDICTS or outcome != verdict:
        blocking = [
            r.get("code", "?")
            for r in verification.get("reasons") or []
            if r.get("severity") in ("reject", "unverified")
        ]
        raise PullRequestError(
            f"verdict is {verdict or outcome or 'missing'}"
            + (f": {', '.join(blocking)}" if blocking else "")
            + " (a pull request needs verified or verified_weak)"
        )
    job = trajectory.get("job")
    if not isinstance(job, dict) or not isinstance(job.get("migration"), str):
        raise PullRequestError("trajectory.json has no job.migration")
    hint = _verify_hint(repo, run_dir, job["migration"])
    if not job.get("diff_sha256"):
        raise PullRequestError(f"the run did not record fix.diff's digest; {hint}")
    if hashlib.sha256(diff).hexdigest() != job["diff_sha256"]:
        raise PullRequestError(
            "fix.diff was changed after it was verified (its sha256 differs from the run's "
            "record); it is not what was checked"
        )
    if not job.get("repo_commit"):
        raise PullRequestError(
            "the run did not record the repo's commit (it was made outside a git checkout, or "
            f"by an older apifix); {hint}"
        )
    if job.get("repo_dirty"):
        raise PullRequestError(
            "the run was made from a repo with uncommitted changes, so no commit matches what "
            f"was verified; commit or discard them, then {hint}"
        )
    return verification, trajectory, diff


def check_checkout(repo: Path, base: str) -> None:
    """Check 3: `repo` is a git work tree's top level, clean, and contains `base`."""
    try:
        top = git_text(repo, ["rev-parse", "--show-toplevel"])
    except GitError:
        raise PullRequestError(f"{repo} is not a git checkout") from None
    if Path(top).resolve() != repo.resolve():
        raise PullRequestError(f"{repo} is inside a git checkout; give its top-level directory")
    if git(repo, ["status", "--porcelain", "--untracked-files=all"]).stdout.strip():
        raise PullRequestError(f"{repo} has uncommitted changes; use a clean checkout")
    if git(repo, ["cat-file", "-e", f"{base}^{{commit}}"], check=False).returncode != 0:
        raise PullRequestError(
            f"the commit the run started from ({base[:12]}) is not in {repo}; "
            "fetch it or use the checkout the run was made from"
        )


def _diff_files(repo: Path, diff: bytes) -> list[ChangedFile]:
    """The files a diff changes, with line counts (git's own parse, so quoting is right)."""
    try:
        out = git(repo, ["apply", "--numstat", "-z"], input=diff).stdout
    except GitError as exc:
        raise PullRequestError(f"the diff cannot be read: {exc}") from None
    files = []
    for entry in out.decode("utf-8", "replace").split("\0"):
        if entry:
            added, deleted, path = entry.split("\t", 2)
            files.append(
                ChangedFile(
                    path,
                    int(added) if added.isdigit() else 0,
                    int(deleted) if deleted.isdigit() else 0,
                )
            )
    return files


def stale_lockfiles(
    repo: Path, base: str, files: list[ChangedFile], trajectory: dict[str, Any]
) -> list[str]:
    """Lockfiles at `base` that the change leaves as they were while its dependency files or
    pins moved: apifix never regenerates a lockfile."""
    changed = {f.path for f in files}
    found = {
        m.group(1)
        for note in trajectory["job"].get("pin_notes") or []
        if (m := _NOT_RECORDED.match(note))
    }
    for path in changed:
        if not is_dependency_file(path):
            continue
        p = PurePosixPath(path)
        for lock in LOCKFILES.get(p.name.lower(), ()):
            rel = (p.parent / lock).as_posix()
            if git(repo, ["cat-file", "-e", f"{base}:{rel}"], check=False).returncode == 0:
                found.add(rel)
    return sorted(found - changed)


def _tree_of_fix(repo: Path, base: str, diff: bytes, index: Path) -> str:
    """The tree of base + diff, built in the temporary index file `index`."""
    try:
        check_no_symlinks(diff.decode("utf-8"))
    except (VerifyError, UnicodeDecodeError) as exc:
        raise PullRequestError(str(exc)) from None
    env = {"GIT_INDEX_FILE": str(index)}
    git(repo, ["read-tree", base], env=env)
    applied = git(
        repo, ["apply", "--cached", "--whitespace=nowarn", "-"], env=env, input=diff, check=False
    )
    if applied.returncode != 0:
        detail = applied.stderr.decode("utf-8", "replace").strip()[-600:]
        raise PullRequestError(f"fix.diff does not apply to {base[:12]}: {detail}")
    return git_text(repo, ["write-tree"], env=env)


def _changed_names(repo: Path, base: str, sha: str) -> list[str]:
    changed = git(repo, ["diff-tree", "-r", "-z", "--no-renames", "--name-only", base, sha]).stdout
    return sorted(n for n in changed.decode("utf-8", "replace").split("\0") if n)


def _build_commit(
    repo: Path, base: str, branch: str, diff: bytes, message: str, who: Identity
) -> tuple[str, list[str]]:
    """Commit base + diff on `branch` with a temporary index. Returns (sha, files changed)."""
    with tempfile.TemporaryDirectory(prefix="apifix-pr-") as tmp:
        tree = _tree_of_fix(repo, base, diff, Path(tmp) / "index")
        sha = git_text(
            repo,
            ["commit-tree", tree, "-p", base],
            identity=who,
            env={"GIT_INDEX_FILE": str(Path(tmp) / "index")},
            input=message.encode("utf-8"),
        )
    return sha, _changed_names(repo, base, sha)


def _is_unfinished_push(
    repo: Path, run: Path, branch: str, sha: str, base: str, diff: bytes, who: Identity
) -> bool:
    """`branch` is the commit an earlier `apifix pr --push` made for this run and never
    opened a PR for: same commit as pr.json records, one commit on `base`, exactly base + diff,
    authored by the env identity."""
    try:
        recorded = json.loads((run / "pr.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(recorded, dict) or recorded.get("pr_url"):
        return False
    if (
        recorded.get("branch") != branch
        or recorded.get("commit") != sha
        or recorded.get("base") != base
    ):
        return False
    parents = git_text(repo, ["rev-list", "--parents", "-n", "1", sha]).split()
    if parents != [sha, base]:
        return False
    author = git_text(repo, ["log", "-1", "--format=%an <%ae>", sha])
    with tempfile.TemporaryDirectory(prefix="apifix-pr-") as tmp:
        tree = _tree_of_fix(repo, base, diff, Path(tmp) / "index")
    return author == f"{who.name} <{who.email}>" and (
        git_text(repo, ["rev-parse", f"{sha}^{{tree}}"]) == tree
    )


def make_pull_request(
    run_dir: str | Path,
    repo: str | Path,
    *,
    env: Mapping[str, str],
    branch: str | None = None,
    for_push: bool = False,
    preflight: Callable[[str, str], None] | None = None,
) -> PullRequestResult:
    """The dry run. Raises PullRequestError (nothing created) with the reason it refused.

    `for_push` adds the checks a published PR needs (an apifix build that can be reproduced) and
    lets a branch left by an unfinished push be reused. `preflight(base, branch)` runs after every
    local check and before anything is created; it may raise to refuse."""
    run_path, repo_path = Path(run_dir).resolve(), Path(repo).resolve()
    if not run_path.is_dir():
        raise PullRequestError(f"not a directory: {run_dir}")
    if not repo_path.is_dir():
        raise PullRequestError(f"not a directory: {repo}")
    verification, trajectory, diff = load_run(run_path, repo_path)
    job = trajectory["job"]
    base = job["repo_commit"]
    check_checkout(repo_path, base)
    who = identity_from_env(env)
    repo_url = repo_url_from_env(env)
    try:
        migration = get_migration(job["migration"])
    except ValueError as exc:
        raise PullRequestError(str(exc)) from None
    dirty = bool((job.get("apifix") or {}).get("dirty"))
    if for_push and dirty:
        raise PullRequestError(
            "the run was made with a dirty apifix build (uncommitted changes in apifix itself), "
            "so the version named in the pull request cannot be reproduced; commit apifix's "
            f"changes, then {_verify_hint(repo_path, run_path, job['migration'])}"
        )
    branch = branch or f"apifix/{migration.name}"
    if git(repo_path, ["check-ref-format", "--branch", branch], check=False).returncode != 0:
        raise PullRequestError(f"{branch!r} is not a valid branch name")
    existing = git_text(
        repo_path, ["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], check=False
    )
    resumed = bool(
        existing
        and for_push
        and _is_unfinished_push(repo_path, run_path, branch, existing, base, diff, who)
    )
    if existing and not resumed:
        raise PullRequestError(
            f"branch {branch} already exists in {repo_path}; delete it (git branch -D {branch}) "
            "or rename it, then run again"
        )

    files = _diff_files(repo_path, diff)
    lockfiles = stale_lockfiles(repo_path, base, files, trajectory)
    text = build_pull_request_text(
        verification,
        trajectory,
        migration,
        files=files,
        base=base,
        diff=diff.decode("utf-8", errors="replace"),
        lockfiles=lockfiles,
        repo_url=repo_url,
    )
    if preflight:
        preflight(base, branch)
    if resumed:
        # An earlier --push made this commit; keep pr-title.txt / pr-body.md (they may have been
        # edited by hand since) and pr.json (it records how far the push got).
        sha, names = existing, _changed_names(repo_path, base, existing)
        outputs = [n for n in OUTPUT_FILES if (run_path / n).is_file()]
    else:
        sha, names = _build_commit(repo_path, base, branch, diff, text.commit_message, who)
        if names != sorted(f.path for f in files):
            raise PullRequestError(
                f"the commit changes {names}, but fix.diff names "
                f"{sorted(f.path for f in files)}; nothing was created"
            )
        # "" as the old value: create the ref only if the branch is still absent.
        git(repo_path, ["update-ref", f"refs/heads/{branch}", sha, ""])
        outputs = _write_outputs(run_path, text, branch, base, sha, names, verification, job, dirty)
    return PullRequestResult(
        branch=branch,
        base=base,
        commit=sha,
        title=text.title,
        files=names,
        verdict=verification["verdict"],
        dirty_apifix=dirty,
        lockfiles=lockfiles,
        outputs=outputs,
        resumed=resumed,
    )


def _write_outputs(
    run: Path,
    text: PullRequestText,
    branch: str,
    base: str,
    sha: str,
    files: list[str],
    verification: dict[str, Any],
    job: dict[str, Any],
    dirty: bool,
) -> list[str]:
    summary = {
        "version": 1,
        "branch": branch,
        "base": base,
        "commit": sha,
        "title": text.title,
        "files": files,
        "verdict": verification["verdict"],
        "apifix": job.get("apifix"),
        "apifix_dirty": dirty,
        "github_user": None,
        "upstream": None,
        "fork": None,
        "draft": None,
        "pushed": False,
        "pr_url": None,
    }
    title, body, summary = redact_local_paths((text.title + "\n", text.body, summary))
    (run / "pr-title.txt").write_text(title, encoding="utf-8")
    (run / "pr-body.md").write_text(body, encoding="utf-8")
    (run / "pr.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return list(OUTPUT_FILES)


def update_summary(run_dir: str | Path, fields: Mapping[str, Any]) -> None:
    """Add `fields` to the run's pr.json (redacted like the rest)."""
    path = Path(run_dir) / "pr.json"
    summary = _load_json(path)
    summary.update(fields)
    path.write_text(json.dumps(redact_local_paths(summary), indent=2) + "\n", encoding="utf-8")
