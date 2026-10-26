"""Pull request text for a verified fix: title, body and commit message.

Written for a maintainer who has never heard of apifix, from verification.json and
trajectory.json (report.md is written for apifix's own users). Pure functions, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apifix.engine.build_info import build_label
from apifix.engine.migrations import Migration

LIST_LIMIT = 10
CODE_WIDTH = 80
DISCLOSURE_CLOSING = "Feel free to close this if it isn't useful."


@dataclass(frozen=True)
class ChangedFile:
    path: str
    added: int
    deleted: int


@dataclass(frozen=True)
class PullRequestText:
    title: str
    body: str
    commit_message: str


def _code(text: str) -> str:
    """Inline code: one line, clipped, with no backticks to break the span."""
    text = " ".join(text.replace("`", "'").split())
    if len(text) > CODE_WIDTH:
        text = text[: CODE_WIDTH - 1] + "…"
    return f"`{text}`"


def _bullets(items: Sequence[str]) -> list[str]:
    """Bullet lines, the first LIST_LIMIT visible and the rest in a collapsed block."""
    lines = [f"- {item}" for item in items[:LIST_LIMIT]]
    rest = items[LIST_LIMIT:]
    if rest:
        lines += [
            f"- and {len(rest)} more:",
            "",
            "<details><summary>the rest</summary>",
            "",
            *[f"  - {item}" for item in rest],
            "",
            "</details>",
        ]
    return lines


def _span(entry: dict[str, Any]) -> str:
    start, end = entry["line"], entry.get("end_line") or entry["line"]
    return f"{entry['file']}:{start}" + (f"-{end}" if end != start else "")


def _hunk_line(entry: dict[str, Any]) -> str:
    what = entry.get("new") or entry.get("old") or ""
    return f"`{_span(entry)}`" + (f": {_code(what)}" if what else "")


def _counts_row(label: str, run: dict[str, Any]) -> str:
    c = run.get("counts") or {}
    if run.get("status") in ("error", "timeout") and not c.get("total"):
        return f"| {label} | did not run ({run.get('status')}) | | | |"
    return (
        f"| {label} | {c.get('passed', 0)} | {c.get('failed', 0)} | "
        f"{c.get('errors', 0)} | {c.get('skipped', 0)} |"
    )


def _build_text(build: dict[str, Any]) -> str:
    """`0.1.0 (git commit abc123...)`; build_label() would describe this process instead of the
    one that made the run when the run recorded none."""
    return build_label(build) if build else "(build not recorded)"


def _who(job: dict[str, Any]) -> str:
    """The sentence that says who wrote the change."""
    if job.get("provider") and job.get("model"):
        return (
            f"An AI model ({job['provider']}/{job['model']}) wrote the change and apifix, "
            "an automated migration tool, verified it"
        )
    return (
        "The change was checked by apifix, an automated migration tool; this run did not "
        "record who or what wrote the diff, so treat it as machine-generated"
    )


def _weak_section(v: dict[str, Any]) -> list[str]:
    weak = [r["message"].splitlines()[0] for r in v["reasons"] if r["severity"] == "weak"]
    cov = v["changed_line_coverage"]
    if cov["available"] and cov["executable"] and not any("coverage" in m.lower() for m in weak):
        weak.append(f"only {cov['executed']}/{cov['executable']} changed lines ran in the tests")
    lines = [
        "> **What the tests did not cover.** This change is only weakly verified: part of it "
        "is code that no test runs, so it needs a careful read.",
        *[f"> - {m}" for m in weak[:LIST_LIMIT]],
    ]
    if v["review_by_hand"]:
        lines.append("> - Not run by any test:")
        shown = v["review_by_hand"][:LIST_LIMIT]
        lines += [f">   - {_hunk_line(h)}" for h in shown]
        if len(v["review_by_hand"]) > len(shown):
            lines.append(f">   - and {len(v['review_by_hand']) - len(shown)} more")
    return lines + [""]


def _what_changed(
    v: dict[str, Any], job: dict[str, Any], files: Sequence[ChangedFile]
) -> list[str]:
    added = sum(f.added for f in files)
    deleted = sum(f.deleted for f in files)
    lines = [f"- {len(files)} file(s) changed (+{added} −{deleted}):"]
    lines += [f"  - `{f.path}`" for f in files[:LIST_LIMIT]]
    if len(files) > LIST_LIMIT:
        lines.append(f"  - and {len(files) - LIST_LIMIT} more")
    before, left = v["findings_before"], len(v["remaining_findings"])
    lines.append(
        f"- Findings (places where the old API is used) resolved: {before - left} of {before}"
        + (f"; {left} left, listed below" if left else "")
    )
    pins = job.get("pin_edits") or []
    if pins:
        lines.append("- Dependency pins set:")
        lines += [f"  - `{p['file']}`: {_code(p['old'])} → {_code(p['new'])}" for p in pins[:5]]
        if len(pins) > 5:
            lines.append(f"  - and {len(pins) - 5} more")
    return lines


def _tests_section(v: dict[str, Any]) -> list[str]:
    runs, delta, cov = v["runs"], v["tests"], v["changed_line_coverage"]
    lines = [
        "| | passed | failed | errors | skipped |",
        "|---|---|---|---|---|",
        _counts_row("Before (unmodified code)", runs["baseline"]),
        _counts_row("After (with this change)", runs["fixed"]),
        "",
    ]
    facts = []
    if delta["fixed_by_change"]:
        facts.append(f"{len(delta['fixed_by_change'])} test(s) failing before now pass")
    if delta["new_tests"]:
        facts.append(f"{len(delta['new_tests'])} new test(s)")
    if cov["available"] and cov["executable"]:
        facts.append(f"changed lines executed by the tests: {cov['executed']}/{cov['executable']}")
    else:
        facts.append("changed-line coverage was not available")
    edits = sorted({e["file"] for e in v["test_edits"]})
    if edits:
        facts.append(f"test files edited: {', '.join(f'`{f}`' for f in edits[:5])}")
    lines.append("; ".join(facts).capitalize() + ".")
    return lines


def _review_section(v: dict[str, Any]) -> list[str]:
    items = []
    if v["verdict"] != "verified_weak":  # the weak notice already lists these hunks
        items += [f"{_hunk_line(h)} (no test runs this)" for h in v["review_by_hand"]]
    items += [
        f"`{e['file']}:{e['line']}`: {e['category']}, still uses {_code(e['old_pattern'])}"
        + ("" if e["changed"] else " (line not changed)")
        for e in v["review_findings"]
    ]
    return ["## Please review by hand", "", *_bullets(items), ""] if items else []


def _lockfile_section(lockfiles: Sequence[str]) -> list[str]:
    if not lockfiles:
        return []
    names = ", ".join(f"`{name}`" for name in lockfiles)
    return [
        "## Lockfiles",
        "",
        f"Not regenerated: {names}. The repo's CI may fail until "
        f"{'it is' if len(lockfiles) == 1 else 'they are'} regenerated.",
        "",
    ]


def _checked_section(v: dict[str, Any], job: dict[str, Any], build: dict[str, Any]) -> list[str]:
    fixed = v["runs"]["fixed"]
    python = fixed.get("python") or "?"
    placeholders = job.get("sandbox_env_placeholders") or fixed.get("env_placeholders") or []
    env = (
        f"; placeholder values (not real keys) were set for {', '.join(placeholders)}"
        if placeholders
        else ""
    )
    lines = [
        "## How it was checked",
        "",
        f"The repo's tests ran in Docker (Python {python}), first on the unmodified code and "
        "then with this change. Dependencies were installed from a generated requirements file "
        "(the only step with network access; it saw no repo code). The tests themselves ran "
        f"with no network and no secrets{env}.",
        "",
        f"Checked by apifix {_build_text(build)}.",
    ]
    if build.get("dirty"):
        lines.append(
            "That apifix build had uncommitted changes, so the commit does not exactly "
            "describe the code that ran."
        )
    return lines + [""]


def _recheck_section(migration: Migration, base: str, repo_url: str | None) -> list[str]:
    if not repo_url:
        return []
    return [
        "## Re-check it",
        "",
        f"With [apifix]({repo_url}) installed, from a checkout of this branch:",
        "",
        "```",
        f"git diff {base[:12]}..HEAD > fix.diff",
        f"apifix verify . --migration {migration.name} --diff fix.diff",
        "```",
        "",
    ]


def title_for(migration: Migration) -> str:
    return f"Migrate to the {migration.target_name} API"


def build_pull_request_text(
    verification: dict[str, Any],
    trajectory: dict[str, Any],
    migration: Migration,
    *,
    files: Sequence[ChangedFile],
    base: str,
    lockfiles: Sequence[str] = (),
    repo_url: str | None = None,
) -> PullRequestText:
    """Title, body and commit message. `lockfiles`: lockfiles the change leaves stale;
    `repo_url`: apifix's home page (the re-check section is left out without one)."""
    job = trajectory["job"]
    build = job.get("apifix") or {}
    v = verification
    weak = v["verdict"] == "verified_weak"
    body = [
        f"This PR moves the code from {migration.source_name} to "
        f"{migration.target_name}. {_who(job)}; review it like any other contribution.",
        "",
    ]
    if weak:
        body += _weak_section(v)
    body += ["## Why", "", migration.pr_why, "", "## What changed", ""]
    body += _what_changed(v, job, files) + ["", "## Tests", ""]
    body += _tests_section(v) + [""]
    body += _review_section(v)
    body += _lockfile_section(lockfiles)
    body += _checked_section(v, job, build)
    body += _recheck_section(migration, base, repo_url)
    body += [DISCLOSURE_CLOSING]
    commit = [
        title_for(migration),
        "",
        f"Moves the code from {migration.source_name} to {migration.target_name}.",
        "",
        f"Written by: {job['provider']}/{job['model']}"
        if job.get("provider") and job.get("model")
        else "Written by: not recorded (the diff came from `apifix verify`)",
        f"Verified by: apifix {_build_text(build)}",
        f"Verdict: {v['verdict']}",
    ]
    return PullRequestText(
        title=title_for(migration),
        body="\n".join(body).rstrip() + "\n",
        commit_message="\n".join(commit) + "\n",
    )
