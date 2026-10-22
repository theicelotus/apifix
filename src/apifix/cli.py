"""Command-line entry point.

apifix scan <path> --migration openai-v1 [--json]
apifix fix <path> --migration openai-v1 [--model M] [--max-cost USD] [--out DIR]
    [--edit-mode agent|whole-file] [--max-file-bytes N]
apifix fix <path> --migration openai-v1 --provider openai-compat --model M [--base-url URL]
    [--strict-tool-calls] [--api-key-env NAME] [--request-timeout S] [--max-output-tokens N]
apifix verify <path> --migration openai-v1 --diff FILE [--out DIR]
                                 apply a saved diff to a copy and verify it (no LLM)
apifix fix|verify ... --sandbox-requirements FILE
                                 install FILE in the sandbox instead of the repo's detected
                                 dependencies (original code and fix; the fix's own
                                 dependency edits still apply)
apifix cleanup [--dry-run]      remove containers/temp dirs left by interrupted runs

fix and verify keep a pip cache for the sandbox's dependency installs
($APIFIX_PIP_CACHE, else ~/.cache/apifix/pip); --no-pip-cache turns it off.
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
        "--strict-tool-calls",
        action="store_true",
        help="openai-compat: only use the server's tool-call field; never parse a reply that "
        "is written as one JSON tool call",
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
