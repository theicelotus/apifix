"""`apifix verify`: a saved diff applied to a copy and verified, with a fake sandbox (no Docker,
no LLM). Needs the `git` binary, which applies the diff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from apifix import cli
from apifix.engine.diffing import unified_diff
from apifix.engine.local_paths import redact_local_paths
from apifix.engine.verify_diff import VerifyError, apply_diff, verify_diff
from tests.test_verifier import (
    APP_NEW,
    FULL_COVERAGE,
    GOOD_FIX,
    ORIGINAL,
    PASSING,
    T_ASK,
    T_STRIP,
    FakeSandbox,
    run,
    write_tree,
)


@pytest.fixture
def saved(tmp_path):
    """The original repo and a diff file holding the good fix (as `fix` writes it)."""
    original = write_tree(tmp_path / "repo", ORIGINAL)
    fixed = write_tree(tmp_path / "fixed", GOOD_FIX)
    diff = tmp_path / "fix.diff"
    diff.write_text(unified_diff(original, fixed))
    return original, diff


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_a_good_diff_is_verified_without_touching_the_repo(saved):
    original, diff = saved
    before = snapshot(original)
    sandbox = FakeSandbox(PASSING, [PASSING])
    result = verify_diff(original, "openai-v1", diff, sandbox=sandbox)
    assert result.outcome == "verified", result.verification.reasons
    assert result.reason == "verified"
    assert snapshot(original) == before
    assert sandbox.baseline_runs == 1
    assert result.diff == diff.read_text()  # the changes as applied
    t = result.trajectory
    assert t["job"]["command"] == "verify"
    assert t["job"]["files_changed"] == ["app.py", "requirements.txt", "tests/test_app.py"]
    assert t["outcome"] == "verified" and t["timing"]["llm_s"] == 0.0
    assert t["verification"]["verdict"] == "verified"
    assert t["baseline"]["status"] == "passed" and t["findings"]
    json.dumps(t)  # JSON-able
    report = result.report_markdown
    assert report.startswith("# apifix verify: Verified\n")
    assert "(3 files changed); applied to a copy of the repo, no LLM used" in report
    assert "## Verification: Verified" in report


def test_imports_nothing_declares_are_listed_before_the_verification(saved):
    original, diff = saved
    (original / "tools").mkdir()
    (original / "tools" / "plot.py").write_text("from PIL import Image\nimport nlpaug\n")
    result = verify_diff(original, "openai-v1", diff, sandbox=FakeSandbox(PASSING, [PASSING]))
    report = result.report_markdown
    section = report.index("## Imported but not declared")
    assert section < report.index("## Verification: Verified")
    assert "- `nlpaug` (pip: `nlpaug`): tools/plot.py\n" in report
    assert "- `PIL` (pip: `pillow`): tools/plot.py\n" in report
    assert result.trajectory["job"]["undeclared_imports"] == [
        {"module": "nlpaug", "distribution": "nlpaug", "files": ["tools/plot.py"]},
        {"module": "PIL", "distribution": "pillow", "files": ["tools/plot.py"]},
    ]
    assert result.outcome == "verified"  # information only


def test_no_section_when_every_import_is_declared(saved):
    original, diff = saved
    result = verify_diff(original, "openai-v1", diff, sandbox=FakeSandbox(PASSING, [PASSING]))
    assert "Imported but not declared" not in result.report_markdown
    assert result.trajectory["job"]["undeclared_imports"] == []


def test_without_a_baseline_failures_leave_it_unverified(saved):
    original, diff = saved
    down = run(status="timeout", reason="dependency install exceeded 900s")
    failing = run(
        (T_ASK, "passed"), (T_STRIP, "failed", "FileNotFoundError: x.pdf"), coverage=FULL_COVERAGE
    )
    result = verify_diff(original, "openai-v1", diff, sandbox=FakeSandbox(down, [failing]))
    assert result.outcome == "unverified"
    assert result.reason == "unverified: no_baseline, unjudged_failures"
    assert result.report_markdown.startswith("# apifix verify: Not verified (not enough evidence)")


def test_a_diff_that_does_not_apply(saved):
    original, diff = saved
    (original / "app.py").write_text(APP_NEW)  # the repo moved on since the diff was made
    with pytest.raises(VerifyError, match="the diff does not apply to the repo: .*app.py"):
        verify_diff(original, "openai-v1", diff, sandbox=FakeSandbox(PASSING, [PASSING]))


def test_paths_outside_the_copy_are_refused(tmp_path):
    work = write_tree(tmp_path / "w" / "repo", {"app.py": "x = 1\n"})
    evil = (
        "diff --git a/../escape.py b/../escape.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/../escape.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('out')\n"
    )
    with pytest.raises(VerifyError, match="does not apply"):
        apply_diff(work, evil)
    assert not (tmp_path / "w" / "escape.py").exists()


def test_symlinks_are_refused(tmp_path):
    work = write_tree(tmp_path / "repo", {"app.py": "x = 1\n"})
    link = (
        "diff --git a/secret b/secret\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        "+++ b/secret\n"
        "@@ -0,0 +1 @@\n"
        "+/etc/passwd\n"
        "\\ No newline at end of file\n"
    )
    with pytest.raises(VerifyError, match="symlink"):
        apply_diff(work, link)
    assert not (work / "secret").exists()


def test_an_empty_diff_or_one_apifix_cannot_see(tmp_path):
    original = write_tree(tmp_path / "repo", ORIGINAL)
    empty = tmp_path / "empty.diff"
    empty.write_text("\n")
    with pytest.raises(VerifyError, match="empty"):
        verify_diff(original, "openai-v1", empty, sandbox=FakeSandbox(PASSING, [PASSING]))
    hidden = tmp_path / "hidden.diff"
    hidden.write_text(
        "diff --git a/venv/x.py b/venv/x.py\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/venv/x.py\n@@ -0,0 +1 @@\n+x = 1\n"
    )
    with pytest.raises(VerifyError, match="changed no file apifix checks"):
        verify_diff(original, "openai-v1", hidden, sandbox=FakeSandbox(PASSING, [PASSING]))


def test_unusable_docker_is_an_error(saved):
    original, diff = saved
    down = run(status="error", reason="docker not usable: permission denied")
    with pytest.raises(VerifyError, match="sandbox unavailable: docker not usable"):
        verify_diff(original, "openai-v1", diff, sandbox=FakeSandbox(down, [down]))


# --- CLI --------------------------------------------------------------------------------------


@pytest.fixture
def fake_sandbox(monkeypatch):
    """Replace the CLI's sandbox; records the configs it was built with."""
    configs: list = []
    sandboxes: list[FakeSandbox] = []

    def make(config=None, *a, **k):
        configs.append(config)
        sandboxes.append(FakeSandbox(PASSING, [PASSING]))
        sandboxes[-1].config = config
        return sandboxes[-1]

    monkeypatch.setattr("apifix.engine.sandbox.Sandbox", make)
    return configs


def test_cli_verify_writes_the_same_files_as_fix(saved, tmp_path, fake_sandbox, capsys):
    original, diff = saved
    out = tmp_path / "out"
    args = ["verify", str(original), "--migration", "openai-v1", "--diff", str(diff)]
    assert cli.main([*args, "--out", str(out)]) == cli.EXIT_CLEAN
    assert sorted(p.name for p in out.iterdir()) == [
        "fix.diff",
        "report.md",
        "trajectory.json",
        "verification.json",
    ]
    assert json.loads((out / "verification.json").read_text())["verdict"] == "verified"
    assert json.loads((out / "trajectory.json").read_text())["job"]["command"] == "verify"
    printed = capsys.readouterr().out
    assert printed.startswith("verified\nno LLM used; wrote fix.diff, report.md")


def test_cli_outputs_write_the_home_dir_as_tilde(saved, tmp_path, fake_sandbox, monkeypatch):
    original, diff = saved
    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / "out"
    args = ["verify", str(original), "--migration", "openai-v1", "--diff", str(diff)]
    assert cli.main([*args, "--out", str(out)]) == cli.EXIT_CLEAN
    job = json.loads((out / "trajectory.json").read_text())["job"]
    assert job["repo"] == "~/" + str(original.relative_to(tmp_path))
    for name in ("trajectory.json", "verification.json", "report.md"):
        assert str(tmp_path) not in (out / name).read_text()


def test_cli_verify_errors(saved, tmp_path, fake_sandbox, capsys):
    original, _ = saved
    bad = tmp_path / "bad.diff"
    bad.write_text("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-nothing like this\n+x\n")
    base = ["verify", str(original), "--migration", "openai-v1"]
    assert cli.main([*base, "--diff", str(bad), "--out", str(tmp_path / "o")]) == cli.EXIT_USAGE
    assert "apifix: the diff does not apply" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
    assert cli.main([*base, "--diff", str(tmp_path / "missing.diff")]) == cli.EXIT_USAGE
    assert "cannot read the diff" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["verify", str(original), "--migration", "openai-v1"])  # --diff is required


def test_cli_pip_cache_default_and_opt_out(saved, tmp_path, fake_sandbox, monkeypatch):
    monkeypatch.setenv("APIFIX_PIP_CACHE", str(tmp_path / "cache"))
    original, diff = saved
    args = ["verify", str(original), "--migration", "openai-v1", "--diff", str(diff)]
    cli.main([*args, "--out", str(tmp_path / "a")])
    cli.main([*args, "--out", str(tmp_path / "b"), "--no-pip-cache"])
    assert [c.pip_cache_dir for c in fake_sandbox] == [tmp_path / "cache", None]


def test_cli_fix_takes_no_pip_cache_too():
    sandbox = cli._sandbox(argparse.Namespace(no_pip_cache=True, sandbox_requirements=None))
    assert sandbox.config.pip_cache_dir is None


def test_git_environment_cannot_redirect_the_apply(saved, tmp_path, monkeypatch):
    original, diff = saved
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    result = verify_diff(original, "openai-v1", diff, sandbox=FakeSandbox(PASSING, [PASSING]))
    assert result.outcome == "verified"
    assert list(other.iterdir()) == []


def test_a_crlf_repo_round_trips_through_fix_diff(tmp_path, fake_sandbox):
    crlf = {k: v.replace("\n", "\r\n") for k, v in ORIGINAL.items()}
    fixed_files = {k: v.replace("\n", "\r\n") for k, v in GOOD_FIX.items()}
    original, fixed = tmp_path / "repo", tmp_path / "fixed"
    for root, files in ((original, crlf), (fixed, fixed_files)):
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(text.encode())
    diff = tmp_path / "fix.diff"
    diff.write_bytes(unified_diff(original, fixed).encode())
    out = tmp_path / "out"
    args = ["verify", str(original), "--migration", "openai-v1", "--diff", str(diff)]
    assert cli.main([*args, "--out", str(out)]) == cli.EXIT_CLEAN
    assert (out / "fix.diff").read_bytes() == diff.read_bytes()


def test_cli_verify_with_sandbox_requirements(saved, tmp_path, fake_sandbox):
    original, diff = saved
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("openai==0.28.1\npytest\n")
    out = tmp_path / "out"
    args = ["verify", str(original), "--migration", "openai-v1", "--diff", str(diff)]
    assert cli.main([*args, "--sandbox-requirements", str(reqs), "--out", str(out)]) == (
        cli.EXIT_CLEAN
    )
    (config,) = fake_sandbox
    assert config.requirements_override.lines == ("openai==0.28.1", "pytest")
    report = (out / "report.md").read_text()
    shown = redact_local_paths(str(reqs.resolve()))  # outputs hide the user's paths
    assert f"- Sandbox dependencies: from `{shown}` (2 requirement(s), sha256 " in report
    job = json.loads((out / "trajectory.json").read_text())["job"]
    assert job["sandbox_requirements"]["lines"] == ["openai==0.28.1", "pytest"]


@pytest.mark.parametrize("command", ["verify", "fix"])
def test_cli_refuses_an_unusable_sandbox_requirements_file(
    saved, tmp_path, fake_sandbox, capsys, command
):
    original, diff = saved
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("-r other.txt\n")
    args = [command, str(original), "--migration", "openai-v1", "--sandbox-requirements", str(reqs)]
    if command == "verify":
        args += ["--diff", str(diff)]
    assert cli.main([*args, "--out", str(tmp_path / "o")]) == cli.EXIT_USAGE
    assert "line 1: option -r is not allowed" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
