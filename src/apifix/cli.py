"""Command-line entry point.

apifix scan <path> --migration openai-v1 [--json]
apifix fix <path> --migration openai-v1 [--model M] [--max-cost USD] [--out DIR]
apifix fix <path> --migration openai-v1 --provider openai-compat --model M [--base-url URL]
apifix cleanup [--dry-run]      remove containers/temp dirs left by interrupted runs
"""

from __future__ import annotations

import argparse
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

EXIT_CLEAN, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2


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
    p_fix.add_argument("--timeout", type=float, help="seconds per LLM request (default 600)")
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
    p_clean = sub.add_parser(
        "cleanup",
        help="remove apifix containers and temp dirs left behind by crashed or killed runs",
    )
    p_clean.add_argument("--dry-run", action="store_true", help="only list what would go")
    args = parser.parse_args(argv)

    if args.command == "fix":
        return _fix(args)
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


def _fix(args: argparse.Namespace) -> int:
    from apifix.engine.agent import AgentConfig, fix
    from apifix.engine.llm import LLMError, get_llm
    from apifix.engine.sandbox import Sandbox

    if args.path.startswith(("http://", "https://", "git@")):
        print("apifix: GitHub URLs are not supported yet; clone locally first", file=sys.stderr)
        return EXIT_USAGE
    if not Path(args.path).is_dir():
        print(f"apifix: not a directory: {args.path}", file=sys.stderr)
        return EXIT_USAGE
    options = {
        key: value
        for key, value in (
            ("base_url", args.base_url),
            ("api_key_env", args.api_key_env),
            ("timeout_s", args.timeout),
        )
        if value is not None
    }
    try:
        llm = get_llm(args.provider, args.model, prices=args.prices, **options)
        notice = getattr(llm, "privacy_notice", None)
        if notice:
            print(notice, file=sys.stderr)
        max_cost = None if args.no_cost_limit else args.max_cost
        config = AgentConfig(max_cost_usd=max_cost, max_retries=args.max_retries)
        result = fix(args.path, args.migration, llm=llm, sandbox=Sandbox(), config=config)
    except (LLMError, ValueError) as exc:
        print(f"apifix: {exc}", file=sys.stderr)
        return EXIT_USAGE

    out = Path(args.out or Path("apifix-out") / datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "fix.diff").write_text(result.diff)
    (out / "report.md").write_text(result.report_markdown)
    verification = result.verification.to_dict() if result.verification else None
    (out / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
    (out / "trajectory.json").write_text(json.dumps(result.trajectory, indent=2) + "\n")
    print(f"{result.outcome}: {result.reason}")
    print(
        f"{result.attempts} attempt(s), {result.usage.total:,} tokens, ${result.cost_usd:.2f}; "
        f"wrote fix.diff, report.md, verification.json, trajectory.json to {out}"
    )
    if result.outcome in ("verified", "verified_weak", "no_findings"):
        return EXIT_CLEAN
    return EXIT_USAGE if result.outcome == "aborted" else EXIT_FINDINGS


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


def _to_text(result: ScanResult) -> str:
    lines = []
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
