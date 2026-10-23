"""openai 0.x module configuration that a rewrite drops or makes ineffective.

0.x code configures the module (`openai.api_key = key`); 1.x's `OpenAI()` reads only its
arguments and the environment, and only the module-level client (`openai.chat.completions...`)
reads `openai.api_key` / `organization` / `base_url` (never `api_base`). Two checks, for
non-test files:

- dropped (A): an assignment of a value 1.x does not read itself must stay (assigned to the
  1.x name) or be passed to a client constructor in the same file;
- ignored (B): a new client constructor call must pass each value the repo configures that
  way (or `**kwargs`), or the configured value never reaches it.

Pure `ast`; used by whole-file mode's pre-apply checks (Migration.rewrite_checker).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from apifix.engine.migrations.openai_v1.python_checks import (
    _collect_aliases,
    _resolve,
    _targets_azure,
    config_target,
    config_value_exempt,
)
from apifix.engine.models import Finding
from apifix.engine.project import is_test_file

# 0.x module attribute -> the 1.x module attribute that holds it (the module-level client
# reads these) and the client constructor keyword
CONFIG = {
    "api_key": ("api_key", "api_key"),
    "organization": ("organization", "organization"),
    "api_base": ("base_url", "base_url"),
    "base_url": ("base_url", "base_url"),
}
CLIENTS = frozenset({"openai.OpenAI", "openai.AsyncOpenAI"})
AZURE_CLIENTS = frozenset({"openai.AzureOpenAI", "openai.AsyncAzureOpenAI"})
_WHAT = {"api_key": "key", "organization": "organization", "base_url": "base URL"}
_MAX_SOURCE = 80

RewriteCheck = Callable[[str, str, str], list[str]]


def _source(node: ast.AST, text: str) -> str:
    segment = " ".join((ast.get_source_segment(text, node) or ast.unparse(node)).split())
    return segment if len(segment) <= _MAX_SOURCE else segment[: _MAX_SOURCE - 3] + "..."


@dataclass(frozen=True)
class Assignment:
    file: str
    line: int
    attr: str  # the module attribute assigned (api_key, organization, api_base, base_url)
    value: ast.expr
    statement: str  # the statement's source, e.g. "openai.api_key = openai_api_key"
    value_source: str

    @property
    def new_attr(self) -> str:
        return CONFIG[self.attr][0]

    @property
    def keyword(self) -> str:
        return CONFIG[self.attr][1]


def config_assignments(file: str, tree: ast.Module, text: str) -> list[Assignment]:
    """Assignments to openai.api_key / organization / api_base / base_url (aliases resolved)
    whose value 1.x does not read itself."""
    aliases = _collect_aliases(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            attr = config_target(target, aliases)
            if attr in CONFIG and not config_value_exempt(attr, node.value):
                out.append(
                    Assignment(
                        file,
                        node.lineno,
                        attr,
                        node.value,
                        _source(node, text),
                        _source(node.value, text),
                    )
                )
    return sorted(out, key=lambda a: a.line)


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _assigned(tree: ast.Module) -> set[tuple[str, str]]:
    """(module attribute, ast.dump of the value) for every assignment to an openai attribute."""
    aliases = _collect_aliases(tree)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                attr = config_target(target, aliases)
                if attr:
                    out.add((attr, ast.dump(node.value)))
    return out


def _constructors(tree: ast.Module, names: frozenset[str]) -> list[ast.Call]:
    aliases = _collect_aliases(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _resolve(node.func, aliases) in names
    ]


def _passed(calls: Sequence[ast.Call]) -> set[tuple[str, str]]:
    """(keyword, ast.dump of the value) passed to the client constructors."""
    return {(kw.arg, ast.dump(kw.value)) for call in calls for kw in call.keywords if kw.arg}


class ConfigChecker:
    """Built once per job from the original repo: its config assignments (non-test files with
    findings) and the non-test files that make API calls (api_call findings)."""

    def __init__(self, root: Path, findings: Sequence[Finding]) -> None:
        self.root = root
        self.assignments: list[Assignment] = []  # non-test, non-Azure files (for check B)
        files = sorted({f.file for f in findings if f.file.endswith(".py")})
        for file in files:
            path = root / file
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            tree = _parse(text)
            if tree is None or is_test_file(file, text):
                continue
            if not _targets_azure(tree, _collect_aliases(tree)):
                self.assignments += config_assignments(file, tree, text)
        self.call_files = sorted(
            {
                f.file
                for f in findings
                if f.category == "api_call"
                and f.file.endswith(".py")
                and not is_test_file(f.file, root=root)
            }
        )

    def __call__(self, path: str, original: str, candidate: str) -> list[str]:
        if not path.endswith(".py") or is_test_file(path, original):
            return []
        old, new = _parse(original), _parse(candidate)
        if old is None or new is None:
            return []
        azure = _targets_azure(old, _collect_aliases(old))
        return self._dropped(path, original, old, new, azure) + (
            [] if azure else self._ignored(path, old, new, candidate)
        )

    # check A

    def _where_calls(self, path: str) -> str:
        if path in self.call_files:
            return (
                "The API calls are in this file: passing the value to the client created here "
                "is simplest"
            )
        if self.call_files:
            where = ", ".join(self.call_files[:3]) + (
                " and others" if len(self.call_files) > 3 else ""
            )
            return f"This file makes no API calls (they are in {where}): keep the assignment"
        return ""

    def _dropped(
        self, path: str, original: str, old: ast.Module, new: ast.Module, azure: bool
    ) -> list[str]:
        assigned = _assigned(new)
        clients = _constructors(new, CLIENTS | AZURE_CLIENTS)
        passed = _passed(clients)
        errors = []
        for a in config_assignments(path, old, original):
            value = ast.dump(a.value)
            keywords = {a.keyword} | (
                {"azure_endpoint"} if azure and a.attr == "api_base" else set()
            )
            new_names = {a.new_attr} | (
                {"azure_endpoint"} if azure and a.attr == "api_base" else set()
            )
            if any((n, value) in assigned for n in new_names) or any(
                (k, value) in passed for k in keywords
            ):
                continue
            what = _WHAT[a.new_attr]
            module = a.statement.split(".", 1)[0]
            if a.attr == "api_base" and ("api_base", value) in assigned:
                errors.append(
                    f"original line {a.line}: `{a.statement}` has no effect in 1.x (nothing "
                    f"reads api_base). Assign `{module}.base_url = {a.value_source}` and call the "
                    f"module-level client, or pass it: `openai.OpenAI(base_url={a.value_source})`"
                )
                continue
            consequence = (
                "so requests would go to api.openai.com in 1.x"
                if a.new_attr == "base_url"
                else f"so that {what} would be silently ignored in 1.x"
            )
            keep = (
                f"assign `{module}.base_url = {a.value_source}`"
                if a.attr == "api_base"
                else "keep the assignment"
            )
            message = (
                f"original line {a.line}: removed `{a.statement}`, {consequence}. "
                f"{keep[0].upper()}{keep[1:]} and call the module-level client "
                f"(`openai.chat.completions.create(...)` reads openai.{a.new_attr}), or pass the "
                f"value to every client (`openai.OpenAI({a.keyword}={a.value_source})`)"
            )
            where = self._where_calls(path)
            errors.append(f"{message}. {where}" if where else message)
        return errors

    # check B

    def _ignored(self, path: str, old: ast.Module, new: ast.Module, candidate: str) -> list[str]:
        if not self.assignments:
            return []
        before = {ast.dump(c) for c in _constructors(old, CLIENTS)}
        errors = []
        for call in _constructors(new, CLIENTS):
            if ast.dump(call) in before or any(kw.arg is None for kw in call.keywords):
                continue
            given = {kw.arg for kw in call.keywords}
            missing: dict[str, list[Assignment]] = {}
            for a in self.assignments:
                if a.keyword not in given:
                    missing.setdefault(a.keyword, []).append(a)
            if not missing:
                continue
            reads = " or ".join(
                f"openai.{keyword}, which "
                + " and ".join(
                    f"{a.file}:{a.line} sets from `{a.value_source}`"
                    + (" (as openai.api_base)" if a.attr == "api_base" else "")
                    for a in group
                )
                for keyword, group in missing.items()
            )
            what = _WHAT[next(iter(missing))] if len(missing) == 1 else "configuration"
            passes = ", ".join(f"{k}=openai.{k}" for k in missing)
            errors.append(
                f"line {call.lineno}: `{_source(call, candidate)}` does not read {reads}, so that "
                f"{what} would be ignored. Call the module-level client instead "
                "(`openai.chat.completions.create(...)`, no client object), or pass it: "
                f"`{ast.unparse(call.func)}({passes})`"
            )
        return errors


def rewrite_checker(root: Path, findings: Sequence[Finding]) -> RewriteCheck:
    return ConfigChecker(root, findings)
