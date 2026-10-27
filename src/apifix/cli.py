"""Command-line entry point.

apifix scan <path> --migration openai-v1 [--json]
apifix fix <path> --migration openai-v1 [--model M] [--max-cost USD] [--out DIR]
    [--edit-mode agent|whole-file] [--max-file-bytes N]
    [--file-edits auto|whole|regions] [--region-threshold BYTES]
apifix fix <path> --migration openai-v1 --provider openai-compat --model M [--base-url URL]
    [--strict-tool-calls] [--api-key-env NAME] [--request-timeout S] [--max-output-tokens N]
    [--reasoning-effort low|medium|high]
apifix verify <path> --migration openai-v1 --diff FILE [--out DIR]
                                 apply a saved diff to a copy and verify it (no LLM)
apifix fix|verify ... --sandbox-requirements FILE
                                 install FILE in the sandbox instead of the repo's detected
                                 dependencies (original code and fix; the fix's own
                                 dependency edits still apply)
apifix pr RUN_DIR --repo CHECKOUT
                                 dry run: a local branch with one commit holding a verified
                                 run's fix, plus pr-title.txt, pr-body.md and pr.json in RUN_DIR
                                 (needs APIFIX_GIT_NAME and APIFIX_GIT_EMAIL, a noreply
                                 address; pushes nothing, no LLM)
apifix pr RUN_DIR --repo CHECKOUT --push [--github-user LOGIN] [--upstream OWNER/REPO] [--ready]
                                 also fork (unless you own the upstream), push the branch and
                                 open a draft pull request with the `gh` CLI (--ready: not a
                                 draft). LOGIN, or $APIFIX_GITHUB_USER, is the account gh must
                                 be logged in as; any other account is refused
apifix cleanup [--dry-run]      remove containers/temp dirs left by interrupted runs

Exit codes: 0 clean (fix: verified; pr: branch made), 1 findings (fix: not verified),
2 usage error or aborted (pr: refused, with the reason on stderr),
3 (fix) already migrated: the findings are all in test files that look out of date with code
already on the new API; nothing was sent to an LLM or run.

report.md, verification.json and trajectory.json write the home directory as ~ (fix.diff is
unchanged).

fix and verify keep a pip cache for the sandbox's dependency installs
($APIFIX_PIP_CACHE, else ~/.cache/apifix/pip); --no-pip-cache turns it off.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from apifix.engine.cleanup import CleanupReport, cleanup
from apifix.engine.docker import DockerCLI
from apifix.engine.migrations import available_migrations
from apifix.engine.models import ScanResult
from apifix.engine.scanner import scan

EXIT_CLEAN, EXIT_FINDINGS, EXIT_USAGE, EXIT_ALREADY_MIGRATED = 0, 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apifix")
    sub = parser.add_subparsers(dest="command", required=True)
    p_scan = sub.add_parser("scan", help="report code affected by a migration")
    p_scan.add_argument("path", help="local repo directory or file")
    p_scan.add_argument("--migration", required=True, choices=available_migrations())
    p_scan.add_argument("--json", action="store_true", help="machine-readable output")
    p_fix = sub.add_parser("fix", help="fix a copy of the repo with an LLM agent, then verify it")
    p_fix.add_argument("path", help="local repo directory (never modified)")
    p_fix.add_argument("--migration", required=True, choices=available_migrations())
    p_fix.add_argument(
        "--provider",
        choices=["anthropic", "openai-compat"],
        help="LLM provider (default: $APIFIX_PROVIDER or anthropic)",
    )
    p_fix.add_argument(
        "--model",
        help="model (default: $APIFIX_MODEL, else claude-opus-5-5; required for openai-compat)",
    )
    p_fix.add_argument(
        "--base-url",
        help="openai-compat endpoint (default: $APIFIX_BASE_URL or http://localhost:11434/v1)",
    )
    p_fix.add_argument(
        "--api-key-env",
        help="openai-compat: env var holding the API key (default: APIFIX_LLM_API_KEY)",
    )
    p_fix.add_argument(
        "--request-timeout",
        "--timeout",
        dest="request_timeout",
        type=float,
        help="seconds to wait for one LLM reply (default 600; openai-compat on a local "
        "endpoint: 3600)",
    )
    p_fix.add_argument(
        "--max-output-tokens",
        type=int,
        help="reply tokens each LLM request allows for (default 16000); lower it when a hosted "
        "free tier counts them against a per-minute token limit",
    )
    p_fix.add_argument(
        "--edit-mode",
        choices=["agent", "whole-file"],
        default="agent",
        help="agent: the model edits through tools (default). whole-file: no tools; the model "
        "rewrites one affected file per request (recommended for small local models)",
    )
    p_fix.add_argument(
        "--max-file-bytes",
        type=int,
        help="whole-file: byte budget for one file plus its read-only context; larger files "
        "are not sent (default 24000)",
    )
    p_fix.add_argument(
        "--file-edits",
        choices=["auto", "whole", "regions"],
        help="whole-file: send each Python file whole or only the regions that need changing "
        "(top-level functions, methods, statements with findings, plus the imports); auto "
        "(default) sends files over --region-threshold as regions",
    )
    p_fix.add_argument(
        "--region-threshold",
        type=int,
        metavar="BYTES",
        help="whole-file, --file-edits auto: Python files over this size are sent as regions "
        "(default 8000)",
    )
    p_fix.add_argument(
        "--strict-tool-calls",
        action="store_true",
        help="openai-compat: only use the server's tool-call field; never parse a reply that "
        "is written as one JSON tool call",
    )
    p_fix.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high"],
        help="openai-compat: sent as reasoning_effort (reasoning models such as gpt-oss); "
        "lower effort leaves more of each reply's tokens for the answer (default: not sent)",
    )
    p_fix.add_argument("--prices", help="TOML file overriding model prices ($APIFIX_PRICES)")
    cost = p_fix.add_mutually_exclusive_group()
    cost.add_argument(
        "--max-cost", type=float, default=5.0, help="US dollar limit for the job (default 5)"
    )
    cost.add_argument(
        "--no-cost-limit",
        action="store_true",
        help="no dollar limit (e.g. an unpriced hosted endpoint); token limits still apply",
    )
    p_fix.add_argument("--max-retries", type=int, default=3)
    p_fix.add_argument("--out", help="output directory (default: ./apifix-out/<timestamp>)")
    p_verify = sub.add_parser(
        "verify", help="apply a saved diff to a copy of the repo and verify it (no LLM)"
    )
    p_verify.add_argument("path", help="local repo directory (never modified)")
    p_verify.add_argument("--migration", required=True, choices=available_migrations())
    p_verify.add_argument(
        "--diff", required=True, help="unified diff to apply (e.g. a previous run's fix.diff)"
    )
    p_verify.add_argument("--out", help="output directory (default: ./apifix-out/<timestamp>)")
    for p in (p_fix, p_verify):
        p.add_argument(
            "--no-pip-cache",
            action="store_true",
            help="download every dependency again instead of using apifix's pip cache",
        )
        p.add_argument(
            "--sandbox-requirements",
            metavar="FILE",
            help="requirements file installed in the sandbox instead of the repo's detected "
            "dependencies (original code and fix); names the fix's dependency edits change "
            "take the fix's version. Only requirements and --index-url / --extra-index-url",
        )
    p_pr = sub.add_parser(
        "pr", help="make a local branch and pull request text for a verified run (pushes nothing)"
    )
    p_pr.add_argument("run_dir", help="output directory of a verified `fix` or `verify` run")
    p_pr.add_argument(
        "--repo", required=True, help="clean checkout of the repo the run was made on"
    )
    p_pr.add_argument(
        "--push",
        action="store_true",
        help="also push the branch (to your fork, or to the upstream you own) and open a draft "
        "pull request with gh",
    )
    p_pr.add_argument(
        "--github-user",
        help="the GitHub login gh must be logged in as ($APIFIX_GITHUB_USER); needed with --push",
    )
    p_pr.add_argument(
        "--upstream",
        metavar="OWNER/REPO",
        help="repository to open the pull request in (default: the checkout's origin)",
    )
    p_pr.add_argument(
        "--ready", action="store_true", help="open the pull request ready for review, not a draft"
    )
    p_clean = sub.add_parser(
        "cleanup",
        help="remove apifix containers and temp dirs left behind by crashed or killed runs",
    )
    p_clean.add_argument("--dry-run", action="store_true", help="only list what would go")
    args = parser.parse_args(argv)

    if args.command == "fix":
        return _fix(args)
    if args.command == "verify":
        return _verify(args)
    if args.command == "pr":
        return _pr(args)
    if args.command == "cleanup":
        report = cleanup(DockerCLI(), dry_run=args.dry_run)
        print(_cleanup_text(report))
        return EXIT_FINDINGS if report.errors else EXIT_CLEAN

    if args.path.startswith(("http://", "https://", "git@")):
        print("apifix: GitHub URLs are not supported yet; clone locally first", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = scan(args.path, args.migration)
    except FileNotFoundError as exc:
        print(f"apifix: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(_to_json(result) if args.json else _to_text(result))
    return EXIT_FINDINGS if result.findings else EXIT_CLEAN


def _sandbox(args: argparse.Namespace):
    """The sandbox for fix/verify; ValueError if --sandbox-requirements is unusable."""
    from apifix.engine.project import load_sandbox_requirements
    from apifix.engine.sandbox import Sandbox, SandboxConfig, default_pip_cache_dir

    cache = None if args.no_pip_cache else default_pip_cache_dir()
    override = (
        load_sandbox_requirements(args.sandbox_requirements) if args.sandbox_requirements else None
    )
    return Sandbox(SandboxConfig(pip_cache_dir=cache, requirements_override=override))


def _out_dir(args: argparse.Namespace) -> Path:
    out = Path(args.out or Path("apifix-out") / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_outputs(
    out: Path, diff: str, report: str, verification: dict | None, trajectory: dict
) -> None:
    from apifix.engine.local_paths import redact_local_paths

    # Outputs get published as evidence: home dir -> ~ (the diff stays byte-for-byte).
    # fix.diff's digest ties a later `apifix pr` to exactly the diff that was verified.
    trajectory["job"]["diff_sha256"] = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    report, verification, trajectory = redact_local_paths((report, verification, trajectory))
    (out / "fix.diff").write_text(diff, newline="")  # CRLF lines of CRLF files stay
    (out / "report.md").write_text(report)
    (out / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    (out / "trajectory.json").write_text(json.dumps(trajectory, indent=2) + "\n")


def _verify(args: argparse.Namespace) -> int:
    from apifix.engine.verify_diff import VerifyError, verify_diff

    if args.path.startswith(("http://", "https://", "git@")):
        print("apifix: GitHub URLs are not supported yet; clone locally first", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = verify_diff(args.path, args.migration, args.diff, sandbox=_sandbox(args))
    except (VerifyError, ValueError) as exc:
        print(f"apifix: {exc}", file=sys.stderr)
        return EXIT_USAGE
    out = _out_dir(args)
    _write_outputs(
        out,
        result.diff,
        result.report_markdown,
        result.verification.to_dict(),
        result.trajectory,
    )
    print(result.reason)
    print(f"no LLM used; wrote fix.diff, report.md, verification.json, trajectory.json to {out}")
    return EXIT_CLEAN if result.outcome in ("verified", "verified_weak") else EXIT_FINDINGS


def _fix(args: argparse.Namespace) -> int:
    from apifix.engine.agent import AgentConfig, fix
    from apifix.engine.llm import LLMError, get_llm

    if args.path.startswith(("http://", "https://", "git@")):
        print("apifix: GitHub URLs are not supported yet; clone locally first", file=sys.stderr)
        return EXIT_USAGE
    if not Path(args.path).is_dir():
        print(f"apifix: not a directory: {args.path}", file=sys.stderr)
        return EXIT_USAGE
    if args.max_file_bytes is not None and args.edit_mode != "whole-file":
        print("apifix: --max-file-bytes only applies to --edit-mode whole-file", file=sys.stderr)
        return EXIT_USAGE
    if args.max_file_bytes is not None and args.max_file_bytes <= 0:
        print("apifix: --max-file-bytes must be positive", file=sys.stderr)
        return EXIT_USAGE
    for flag, value in (
        ("--file-edits", args.file_edits),
        ("--region-threshold", args.region_threshold),
    ):
        if value is not None and args.edit_mode != "whole-file":
            print(f"apifix: {flag} only applies to --edit-mode whole-file", file=sys.stderr)
            return EXIT_USAGE
    if args.region_threshold is not None and args.region_threshold <= 0:
        print("apifix: --region-threshold must be positive", file=sys.stderr)
        return EXIT_USAGE
    if args.request_timeout is not None and args.request_timeout <= 0:
        print("apifix: --request-timeout must be positive", file=sys.stderr)
        return EXIT_USAGE
    if args.max_output_tokens is not None and args.max_output_tokens <= 0:
        print("apifix: --max-output-tokens must be positive", file=sys.stderr)
        return EXIT_USAGE
    options = {
        key: value
        for key, value in (
            ("base_url", args.base_url),
            ("api_key_env", args.api_key_env),
            ("timeout_s", args.request_timeout),
            ("strict_tool_calls", args.strict_tool_calls or None),
            ("reasoning_effort", args.reasoning_effort),
        )
        if value is not None
    }
    try:
        sandbox = _sandbox(args)  # checks --sandbox-requirements before any LLM setup
        llm = get_llm(args.provider, args.model, prices=args.prices, **options)
        notice = getattr(llm, "privacy_notice", None)
        if notice:
            print(notice, file=sys.stderr)
        max_cost = None if args.no_cost_limit else args.max_cost
        limits = {
            key: value
            for key, value in (
                ("whole_file_max_bytes", args.max_file_bytes),
                ("max_output_tokens", args.max_output_tokens),
                ("file_edits", args.file_edits),
                ("region_threshold_bytes", args.region_threshold),
            )
            if value is not None
        }
        config = AgentConfig(
            max_cost_usd=max_cost,
            max_retries=args.max_retries,
            edit_mode=args.edit_mode,
            **limits,
        )
        result = fix(args.path, args.migration, llm=llm, sandbox=sandbox, config=config)
    except (LLMError, ValueError) as exc:
        print(f"apifix: {exc}", file=sys.stderr)
        return EXIT_USAGE

    out = _out_dir(args)
    verification = result.verification.to_dict() if result.verification else None
    _write_outputs(out, result.diff, result.report_markdown, verification, result.trajectory)
    print(f"{result.outcome}: {result.reason}")
    if result.outcome == "already_migrated":
        print(_findings_text(result.trajectory["findings"]))
    print(
        f"{result.attempts} attempt(s), {result.usage.total:,} tokens, ${result.cost_usd:.2f}; "
        f"wrote fix.diff, report.md, verification.json, trajectory.json to {out}"
    )
    if result.outcome in ("verified", "verified_weak", "no_findings"):
        return EXIT_CLEAN
    if result.outcome == "already_migrated":
        return EXIT_ALREADY_MIGRATED
    return EXIT_USAGE if result.outcome == "aborted" else EXIT_FINDINGS


def _plural(n: int, noun: str) -> str:
    """`1 file`, `3 files`."""
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _pr(args: argparse.Namespace) -> int:
    import os
    from pathlib import Path

    from apifix.engine.github import GitHub, GitHubError, PullRequestPublisher
    from apifix.engine.pull_request import PullRequestError, make_pull_request

    def refuse(reason: str) -> int:
        print(f"apifix: refusing to make a pull request: {reason}", file=sys.stderr)
        return EXIT_USAGE

    user = args.github_user or os.environ.get("APIFIX_GITHUB_USER", "")
    if not args.push:
        used = [
            flag
            for flag, given in (
                ("--github-user", args.github_user),
                ("--upstream", args.upstream),
                ("--ready", args.ready),
            )
            if given
        ]
        if used:
            return refuse(f"{', '.join(used)} only apply with --push")
    elif not user.strip():
        return refuse(
            "--push needs the GitHub login you expect gh to be logged in as: pass --github-user "
            "LOGIN or set APIFIX_GITHUB_USER (a wrong account is refused)"
        )
    publisher = None
    try:
        if args.push:
            publisher = PullRequestPublisher(
                GitHub(),
                Path(args.repo).resolve(),
                expected_user=user,
                upstream=args.upstream,
                ready=args.ready,
            )
        result = make_pull_request(
            args.run_dir,
            args.repo,
            env=os.environ,
            for_push=args.push,
            preflight=publisher.preflight if publisher else None,
        )
    except (PullRequestError, GitHubError) as exc:
        return refuse(str(exc))
    print(f"{result.verdict}: branch {result.branch} ({result.commit[:12]}) in {args.repo}")
    verb = ""
    if result.resumed:
        verb = "reused the branch " + (
            "from the dry run: " if result.from_dry_run else "of an unfinished push: "
        )
    print(f"{verb}{_plural(len(result.files), 'file')} in one commit on {result.base[:12]}")
    if result.dirty_apifix:
        print("note: the run was made with a dirty apifix build; the PR body says so")
    print(f"wrote {', '.join(result.outputs)} to {args.run_dir}")
    if not publisher:
        print("nothing was pushed")
        return EXIT_CLEAN
    print(f"GitHub account: {publisher.user} (as gh is logged in)")
    try:
        done = publisher.publish(Path(args.run_dir).resolve(), result.branch)
    except GitHubError as exc:
        print(f"apifix: the pull request was not opened: {exc}", file=sys.stderr)
        return EXIT_USAGE
    where = f"{done.fork} (fork of {done.upstream})" if done.fork else done.upstream
    print(f"pushed {result.branch} to {where}")
    print(f"opened a {'draft ' if done.draft else ''}pull request: {done.pr_url}")
    return EXIT_CLEAN


def _cleanup_text(report: CleanupReport) -> str:
    verb = "would remove" if report.dry_run else "removed"
    lines = [f"{verb} container {c}" for c in report.removed_containers]
    lines += [f"{verb} temp dir {d}" for d in report.removed_dirs]
    lines += [f"skipped {s}" for s in report.skipped]
    lines += [f"error: {e}" for e in report.errors]
    summary = (
        f"{len(report.removed_containers)} container(s), {len(report.removed_dirs)} temp dir(s)"
    )
    lines.append(f"{verb} {summary}" + ("" if lines else ": nothing left over"))
    return "\n".join(lines)


def _to_json(result: ScanResult) -> str:
    return json.dumps(asdict(result), indent=2)


def _findings_text(findings: list[dict]) -> str:
    """One line per finding (fix's console output for an already-migrated repo)."""
    return "\n".join(
        f"  {f['file']}:{f['line']}:{f['col']}: {f['category']} [{f['confidence']}] "
        f"{f['old_pattern']}"
        for f in findings
    )


def _to_text(result: ScanResult) -> str:
    lines = []
    if result.already_migrated:
        lines.append(result.already_migrated.message)
        lines += [f"  {line}" for line in result.already_migrated.report_lines()]
    for f in result.findings:
        lines.append(f"{f.file}:{f.line}:{f.col}: {f.category} [{f.confidence}]")
        lines.append(f"    old: {f.old_pattern}")
        lines.append(f"    fix: {f.suggestion}")
    for e in result.parse_errors:
        lines.append(f"{e.file}: could not parse ({e.error})")
    files = len({f.file for f in result.findings})
    lines.append(
        f"{len(result.findings)} finding(s) in {files} file(s); "
        f"{result.files_scanned} file(s) scanned, {len(result.parse_errors)} parse error(s)"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
