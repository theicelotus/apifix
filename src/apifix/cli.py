"""Command-line entry point.

apifix scan <path> --migration openai-v1 [--json]
apifix cleanup [--dry-run]      remove containers/temp dirs left by interrupted runs
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

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
    p_clean = sub.add_parser(
        "cleanup",
        help="remove apifix containers and temp dirs left behind by crashed or killed runs",
    )
    p_clean.add_argument("--dry-run", action="store_true", help="only list what would go")
    args = parser.parse_args(argv)

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
