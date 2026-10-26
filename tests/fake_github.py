"""A fake `gh` and `git` for tests of `apifix pr --push`. They are the only programs on PATH, so a
test can never reach the real gh, GitHub or a real `git push`.

`gh` answers the handful of commands apifix uses from a JSON state file (a tiny GitHub). `git`
records a `push` and does nothing; any other git command runs the real git, so branches and
commits in the test's temp repos are real. Every call is appended to a log (argv, and whether a
token was in the environment, never its value) for the test to read.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
from pathlib import Path

FAKE_TOKEN = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE0123456789"

_SCRIPT = r"""#!{python}
import json, os, sys
from urllib.parse import parse_qs, urlsplit

root = os.environ["FAKE_GH_DIR"]
state_path = os.path.join(root, "state.json")
state = json.load(open(state_path))
tool = os.path.basename(sys.argv[0])
argv = sys.argv[1:]
token = os.environ.get("GH_TOKEN", "")


def log(extra=None):
    entry = {{"tool": tool, "argv": argv, "has_token": bool(token)}}
    entry["gh_host"] = os.environ.get("GH_HOST")
    entry.update(extra or {{}})
    with open(os.path.join(root, "log.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")


def save():
    json.dump(state, open(state_path, "w"))


def fail(message, code=1):
    sys.stderr.write(message.replace("{{token}}", token) + "\n")
    raise SystemExit(code)


def subcommand(args):
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("-c", "-C"):
            skip = True
        elif not a.startswith("-"):
            out.append(a)
    return out


if tool == "git":
    sub = subcommand(argv)
    if sub[:1] == ["push"]:
        log({{"push": True}})
        if state.get("push_error"):
            fail(state["push_error"])
        raise SystemExit(0)
    os.execv(os.environ["FAKE_REAL_GIT"], [os.environ["FAKE_REAL_GIT"]] + argv)

log()
if argv[:2] == ["auth", "status"]:
    if not state["logged_in"]:
        fail("You are not logged in to any GitHub hosts. {{token}}")
    raise SystemExit(0)
if argv[:2] == ["api", "user"]:
    print(state["login"])
    raise SystemExit(0)
if argv[0] == "api":
    path = [a for a in argv[1:] if a.startswith("repos/")][0]
    parts = urlsplit(path)
    segs = parts.path.split("/")
    if len(segs) == 3:
        repo = state["repos"].get(("/".join(segs[1:3])).lower())
        if state.get("view_failures", 0) > 0 and repo and repo.get("new"):
            state["view_failures"] -= 1
            save()
            fail("gh: Not Found (HTTP 404)")
        if not repo:
            fail("gh: Not Found (HTTP 404) {{token}}")
        print(json.dumps(repo))
        raise SystemExit(0)
    if segs[3] == "compare":
        status = state["compare"]
        if status == "missing":
            fail("gh: No commit found for SHA (HTTP 422)")
        print(status)
        raise SystemExit(0)
    if segs[3] == "pulls":
        head = parse_qs(parts.query)["head"][0]
        for pr in state["open_prs"]:
            if pr["head"] == head:
                print(pr["url"])
        raise SystemExit(0)
if argv[:2] == ["repo", "fork"]:
    if state.get("fork_error"):
        fail(state["fork_error"])
    upstream = argv[2]
    name = state["login"] + "/" + upstream.split("/")[1]
    if name.lower() not in state["repos"]:
        base = state["repos"][upstream.lower()]
        state["repos"][name.lower()] = dict(
            base, full_name=name, fork=True, parent={{"full_name": upstream}}, new=True,
            permissions={{"push": True}}, private=False,
        )
        save()
    raise SystemExit(0)
if argv[:2] == ["pr", "create"]:
    if state.get("pr_error"):
        fail(state["pr_error"])
    state.setdefault("created", []).append(argv)
    save()
    print("Creating pull request for you")
    print(state["pr_url"])
    raise SystemExit(0)
fail("fake gh: unsupported command: " + " ".join(argv), 3)
"""


def _repo(full_name: str, *, push: bool = True, private: bool = False) -> dict:
    return {
        "full_name": full_name,
        "default_branch": "main",
        "fork": False,
        "parent": None,
        "permissions": {"push": push},
        "private": private,
    }


class FakeGitHub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"

    @property
    def state(self) -> dict:
        return json.loads((self.root / "state.json").read_text())

    def set(self, **changes) -> None:
        state = self.state
        state.update(changes)
        (self.root / "state.json").write_text(json.dumps(state))

    def add_repo(self, full_name: str, **kw) -> None:
        repos = self.state["repos"]
        repos[full_name.lower()] = _repo(full_name, **kw)
        self.set(repos=repos)

    def calls(self, tool: str | None = None) -> list[dict]:
        path = self.root / "log.jsonl"
        entries = (
            [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        )
        return [e for e in entries if tool is None or e["tool"] == tool]

    def gh_calls(self) -> list[list[str]]:
        return [e["argv"] for e in self.calls("gh")]

    def pushes(self) -> list[dict]:
        return [e for e in self.calls("git") if e.get("push")]

    def created_prs(self) -> list[list[str]]:
        return self.state.get("created", [])


def install(tmp_path: Path, monkeypatch, **state) -> FakeGitHub:
    """Put the fakes alone on PATH. `state` overrides the defaults (a user `theicelotus` logged in,
    `owner/repo` upstream with main, the base commit an ancestor of main, no open PRs)."""
    real_git = shutil.which("git")
    assert real_git, "the tests need the git binary"
    root = tmp_path / "fake-github"
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("gh", "git"):
        script = bin_dir / name
        script.write_text(_SCRIPT.format(python=sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    fake = FakeGitHub(root)
    defaults = {
        "login": "theicelotus",
        "logged_in": True,
        "repos": {"owner/repo": _repo("owner/repo")},
        "compare": "behind",
        "open_prs": [],
        "pr_url": "https://github.com/owner/repo/pull/7",
    }
    (root / "state.json").write_text(json.dumps({**defaults, **state}))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("FAKE_GH_DIR", str(root))
    monkeypatch.setenv("FAKE_REAL_GIT", real_git)
    monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("APIFIX_GITHUB_USER", raising=False)
    monkeypatch.delenv("GH_HOST", raising=False)
    return fake


def token_free(text: str) -> bool:
    return FAKE_TOKEN not in text and FAKE_TOKEN[4:] not in text


def tree_text(path: Path) -> str:
    """All text under `path` (for "the token is in no written file")."""
    parts = []
    for p in sorted(path.rglob("*")):
        if p.is_file() and ".git" not in p.parts:
            parts.append(p.read_text(errors="replace"))
    return "\n".join(parts)
