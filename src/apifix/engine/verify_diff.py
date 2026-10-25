"""`apifix verify`: apply a saved diff to a copy of a repo and verify it the way `fix` verifies
an attempt (baseline run, sandbox run of the fixed copy, the verifier's checks), with no LLM.
For re-checking a fix later, e.g. after the original code's install failed on a slow network.
The repo itself is never modified.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apifix.engine.build_info import apifix_build, build_label
from apifix.engine.diffing import diff_trees, unified_diff
from apifix.engine.imports import (
    UndeclaredImport,
    undeclared_imports,
    undeclared_record,
    undeclared_section,
)
from apifix.engine.migrations import Migration, get_migration
from apifix.engine.project import ci_env_vars
from apifix.engine.sandbox import Sandbox, SandboxResult
from apifix.engine.scanner import scan
from apifix.engine.verifier import (
    Verification,
    VerifierConfig,
    downloads_line,
    requirements_override,
    run_summary,
    sandbox_requirements_lines,
    sandbox_requirements_record,
    verify,
)
from apifix.engine.walk import copy_repo

TRAJECTORY_VERSION = 1
GIT_TIMEOUT_S = 60
# git diff headers that make a path a symlink (mode 120000): refused, a link could point
# anywhere on the host.
_SYMLINK_MODE = re.compile(r"^(?:new file mode|new mode|old mode|deleted file mode) 120000\b", re.M)

_TITLES = {
    "verified": "Verified",
    "verified_weak": "Verified, with weak evidence",
    "rejected": "Not verified (rejected)",
    "unverified": "Not verified (not enough evidence)",
}


class VerifyError(Exception):
    """The verification could not start (the diff does not apply, Docker is unusable, ...)."""


@dataclass
class VerifyResult:
    outcome: str  # the verdict
    reason: str
    diff: str  # the changes as applied (regenerated from the trees)
    verification: Verification
    report_markdown: str
    trajectory: dict[str, Any]


def apply_diff(work: Path, diff: str) -> None:
    """Apply a unified diff (git format, or plain with a/ b/ prefixes) to the tree `work` with
    `git apply`. git does not look above `work` for a repository, ignores the user's git
    config, and refuses paths outside `work`; diffs that create or change symlinks are
    refused here."""
    if not diff.strip():
        raise VerifyError("the diff is empty")
    if _SYMLINK_MODE.search(diff):
        raise VerifyError("the diff creates or changes a symlink, which apifix does not apply")
    redirect = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS"}
    env = {
        **{k: v for k, v in os.environ.items() if k not in redirect},
        "GIT_CEILING_DIRECTORIES": str(work.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    try:
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=diff.encode("utf-8"),
            cwd=work,
            env=env,
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise VerifyError("`git` is needed to apply the diff, and it is not installed") from None
    except subprocess.TimeoutExpired:
        raise VerifyError(f"git apply did not finish in {GIT_TIMEOUT_S} s") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-2000:]
        raise VerifyError(f"the diff does not apply to the repo: {detail or 'git apply failed'}")


def verify_diff(
    repo: str | Path,
    migration: str | Migration,
    diff_path: str | Path,
    *,
    sandbox: Sandbox,
    config: VerifierConfig | None = None,
) -> VerifyResult:
    """Verify the changes in `diff_path` against `repo`. Raises VerifyError if they cannot be
    applied or the sandbox is unusable."""
    original = Path(repo).resolve()
    if not original.is_dir():
        raise VerifyError(f"not a directory: {repo}")
    try:
        with Path(diff_path).open(encoding="utf-8", newline="") as f:  # CRLF files' lines
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise VerifyError(f"cannot read the diff {diff_path}: {exc}") from None
    mig = get_migration(migration) if isinstance(migration, str) else migration
    started = datetime.now(UTC)
    start = time.monotonic()
    findings = scan(original, mig).findings
    ci_env = ci_env_vars(original, mig.ci_env_placeholders)
    env = {name: mig.ci_env_placeholders[name] for name in ci_env}
    with tempfile.TemporaryDirectory(prefix="apifix-verify-") as tmp:
        work = Path(tmp) / original.name
        copy_repo(original, work)
        apply_diff(work, text)
        changes = diff_trees(original, work)
        if not changes:
            raise VerifyError(
                "the diff applied but changed no file apifix checks (files in .git, "
                "virtualenvs and build dirs are ignored)"
            )
        applied = unified_diff(original, work, changes)

        undeclared = undeclared_imports(original, requirements_override(sandbox))
        t0 = time.monotonic()
        baseline = sandbox.run(original, env=env)
        baseline_s = time.monotonic() - t0
        if baseline.status == "error" and baseline.reason.startswith("docker not usable"):
            raise VerifyError(f"sandbox unavailable: {baseline.reason}")
        t1 = time.monotonic()
        v = verify(
            original, work, mig, sandbox=sandbox, baseline=baseline, config=config, sandbox_env=env
        )
        verify_s = time.monotonic() - t1
    duration = time.monotonic() - start

    blocking = [r.code for r in v.reasons if r.severity in ("reject", "unverified", "weak")]
    reason = f"{v.verdict}: {', '.join(dict.fromkeys(blocking))}" if blocking else v.verdict
    trajectory = {
        "version": TRAJECTORY_VERSION,
        "job": {
            "command": "verify",
            "apifix": apifix_build(),
            "repo": str(original),
            "migration": mig.name,
            "diff": str(Path(diff_path).resolve()),
            "files_changed": [ch.path for ch in changes],
            "sandbox_env_placeholders": ci_env,
            "sandbox_requirements": sandbox_requirements_record(sandbox),
            "undeclared_imports": undeclared_record(undeclared),
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
        },
        "findings": [asdict(f) for f in findings],
        "baseline": run_summary(baseline),
        "baseline_duration_s": round(baseline_s, 3),
        "verification": v.to_dict(),
        "verification_duration_s": round(verify_s, 3),
        "outcome": v.verdict,
        "reason": reason,
        "timing": {"llm_s": 0.0, "sandbox_s": round(baseline_s + verify_s, 3)},
        "duration_s": round(duration, 3),
    }
    report = _report(
        v,
        mig,
        Path(diff_path),
        len(changes),
        reason,
        ci_env,
        duration,
        baseline_s,
        verify_s,
        sandbox_requirements_lines(sandbox),
        undeclared,
    )
    return VerifyResult(v.verdict, reason, applied, v, report, trajectory)


def _seconds(s: float) -> str:
    return f"{s:,.0f} s" if s >= 10 else f"{s:.1f} s"


def _report(
    v: Verification,
    mig: Migration,
    diff_path: Path,
    files: int,
    reason: str,
    ci_env: dict[str, str],
    duration: float,
    baseline_s: float,
    verify_s: float,
    dependency_lines: Sequence[str] = (),
    undeclared: Sequence[UndeclaredImport] = (),
) -> str:
    lines = [
        f"# apifix verify: {_TITLES[v.verdict]}",
        "",
        f"- Migration: `{mig.name}` ({mig.summary})",
        f"- apifix: {build_label()}",
        f"- Diff: `{diff_path}` ({files} file{'' if files == 1 else 's'} changed); applied to a "
        "copy of the repo, no LLM used",
        f"- Outcome: {reason}",
        f"- Time: {_seconds(duration)} total; sandbox {_seconds(baseline_s + verify_s)} "
        f"(baseline {_seconds(baseline_s)}, verification {_seconds(verify_s)})",
    ]
    for name, workflow in ci_env.items():
        lines.append(
            f"- Sandbox env: `{name}` set to a placeholder in the test phase, because the repo's "
            f"CI sets it (`{workflow}`); the sandbox has no network"
        )
    lines += dependency_lines
    retried = [
        label
        for label, run in (("baseline", v.baseline), ("fixed", v.fixed))
        if run.install_retried
    ]
    if retried:
        lines.append(
            "- Dependency install retried once after a failure that looked transient: "
            + ", ".join(retried)
        )
    runs: list[tuple[str, SandboxResult]] = [("baseline", v.baseline), ("fixed", v.fixed)]
    downloads = downloads_line(runs)
    if downloads:
        lines.append(downloads)
    lines += ["", *undeclared_section(list(undeclared)), v.report_markdown()]
    return "\n".join(lines).rstrip() + "\n"
