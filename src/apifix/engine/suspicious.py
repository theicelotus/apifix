"""Flag code a fix added that a library migration has no reason to add.

A defence against prompt injection: the fix agent reads untrusted repository text, so an
instruction hidden in a comment could make it add an exfiltration call or a payload. Only
additions are flagged (compared with the original file), and only as warnings: a reviewer
decides. Detected: new imports outside the migrated library, network / process calls,
dynamic code execution, long encoded string literals, and new dependencies.
"""

from __future__ import annotations

import ast
import configparser
import re
from collections import Counter
from dataclasses import dataclass

from apifix.engine.api_names import in_modules, resolve
from apifix.engine.dependencies import normalize_name, parse_dependency_file
from apifix.engine.tampering import dotted_name

NETWORK_MODULES = (
    "requests",
    "urllib",
    "urllib3",
    "http.client",
    "httpx",
    "aiohttp",
    "socket",
    "ftplib",
    "smtplib",
    "telnetlib",
)
PROCESS_MODULES = ("subprocess", "pty")
PROCESS_FUNCTIONS = ("os.system", "os.popen", "os.exec", "os.spawn", "os.posix_spawn")
# Data types of network libraries: constructing them does no I/O (openai 1.x errors need them).
NETWORK_DATA_TYPES = {
    "httpx.Request",
    "httpx.Response",
    "httpx.Headers",
    "httpx.URL",
    "httpx.Timeout",
    "httpx.Limits",
    "requests.Response",
}
DYNAMIC_CODE = {"eval", "exec", "compile", "__import__", "importlib.import_module"}
# Stdlib modules without I/O that migrated code and test mocks commonly need.
HARMLESS_IMPORTS = {
    "__future__",
    "types",
    "typing",
    "functools",
    "dataclasses",
    "collections",
    "unittest.mock",
}
_ENCODED = re.compile(r"^(?:[A-Za-z0-9+/_-]{80,}={0,2}|(?:[0-9a-fA-F]{2}){40,})$")
MIN_ESCAPED_BYTES = 80


@dataclass(frozen=True)
class SuspiciousEdit:
    file: str
    line: int
    # new_import | network_call | process_call | dynamic_code | encoded_literal | new_dependency
    kind: str
    detail: str


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> canonical dotted name, from the file's imports."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    top = a.name.split(".", 1)[0]
                    out[top] = top
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _imports(tree: ast.Module) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found += [(f"{node.module}.{a.name}", node.lineno) for a in node.names]
    return found


def _prefixes(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _calls(tree: ast.Module) -> list[tuple[str, str, int]]:
    """(kind, canonical name, line) of every risky call."""
    aliases = _aliases(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        raw = dotted_name(node.func)
        if not raw:
            continue
        name = resolve(raw, aliases)
        if name in DYNAMIC_CODE or raw in DYNAMIC_CODE:
            found.append(("dynamic_code", name, node.lineno))
        elif in_modules(name, NETWORK_MODULES) and name not in NETWORK_DATA_TYPES:
            found.append(("network_call", name, node.lineno))
        elif in_modules(name, PROCESS_MODULES) or name.startswith(PROCESS_FUNCTIONS):
            found.append(("process_call", name, node.lineno))
    return found


def _encoded_literals(tree: ast.Module) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if isinstance(value, str):
            compact = value.replace("\r", "").replace("\n", "")  # wrapped base64
            if _ENCODED.match(compact) and any(c.isdigit() for c in compact):
                found.append((compact[:24], node.lineno))
        elif isinstance(value, bytes) and len(value) >= MIN_ESCAPED_BYTES:
            if sum(b < 32 or b > 126 for b in value) > len(value) // 4:
                found.append((repr(value[:16]), node.lineno))
    return found


def _new(old: list, new: list, key) -> list:
    """Items of `new` beyond what `old` already had (multiset difference by key)."""
    budget = Counter(key(x) for x in old)
    out = []
    for item in new:
        k = key(item)
        if budget[k]:
            budget[k] -= 1
        else:
            out.append(item)
    return out


def check_python(
    file: str,
    old_src: str | None,
    new_src: str | None,
    api_modules: frozenset[str],
    companion_modules: frozenset[str] = frozenset(),
) -> list[SuspiciousEdit]:
    """`api_modules` and `companion_modules` (libraries whose types the new API uses, e.g.
    httpx for openai) may be imported freely; calls into them are still checked."""
    if new_src is None:
        return []
    try:
        new_tree = ast.parse(new_src)
    except SyntaxError:
        return []  # reported elsewhere (syntax_error / test_file_unparseable)
    try:
        old_tree = ast.parse(old_src) if old_src is not None else ast.Module([], [])
    except SyntaxError:
        old_tree = ast.Module([], [])

    edits = []
    free = api_modules | companion_modules
    old_imports = {m for m, _ in _imports(old_tree)}
    for name, line in _imports(new_tree):
        prefixes = _prefixes(name)
        if (
            name in old_imports
            or in_modules(name, free)
            or any(p in HARMLESS_IMPORTS for p in prefixes)
        ):
            continue
        old_imports.add(name)  # report each new name once
        edits.append(SuspiciousEdit(file, line, "new_import", f"new import `{name}`"))

    for kind, name, line in _new(_calls(old_tree), _calls(new_tree), lambda c: (c[0], c[1])):
        what = {
            "network_call": "network call",
            "process_call": "process call",
            "dynamic_code": "dynamic code execution",
        }[kind]
        edits.append(SuspiciousEdit(file, line, kind, f"new {what} `{name}(...)`"))

    for text, line in _new(
        _encoded_literals(old_tree), _encoded_literals(new_tree), lambda e: e[0]
    ):
        edits.append(
            SuspiciousEdit(file, line, "encoded_literal", f"long encoded literal `{text}...`")
        )
    return sorted(edits, key=lambda e: (e.line, e.kind))


def check_dependencies(
    file: str, old_text: str | None, new_text: str | None, api_modules: frozenset[str]
) -> list[SuspiciousEdit]:
    if new_text is None:
        return []
    try:
        old = parse_dependency_file(file, old_text) if old_text is not None else []
        new = parse_dependency_file(file, new_text)
    except (ValueError, SyntaxError, configparser.Error):
        return []
    allowed = {normalize_name(m) for m in api_modules}
    before = {d.name for d in old}
    return [
        SuspiciousEdit(file, d.line, "new_dependency", f"new dependency `{d.raw}`")
        for d in new
        if d.name not in before and d.name not in allowed
    ]
