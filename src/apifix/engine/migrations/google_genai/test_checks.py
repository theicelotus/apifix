"""The `patches` rewrite check: a rewritten test's `mock.patch` targets must name something the
new SDK still has.

`google.generativeai.configure` and `.GenerativeModel` are gone (module-level functions became a
client), so a test's patch target left pointing at them fails with `AttributeError: <module
'google.genai'> does not have the attribute 'configure'` well into a run, not at collection; this
check catches it from the rewritten source instead. `autospec=True` on the client class is
flagged too: its resources are properties (code_checks.autospec_client_problems catches this
generically for non-test code already using the client, but a test's own `patch(..., autospec=
True)` call is specific to this check)."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from pathlib import Path

from apifix.engine.models import Finding
from apifix.engine.project import is_test_file

# Names that existed on google.generativeai (the module) and are gone from google.genai: a
# patch target ending in one of these, through a chain that resolves to the old module or to
# `<module under test>.genai`, is a patch of something that no longer exists.
_REMOVED_MODULE_NAMES = frozenset({"configure", "GenerativeModel", "embed_content", "list_models"})


def _patch_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _patch_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _patch_name(node.func) in ("patch", "object")
    ]


def _target_string(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _autospec(call: ast.Call) -> bool:
    return any(
        kw.arg == "autospec" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in call.keywords
    )


def _patch_errors(path: str, candidate: str) -> list[str]:
    if not path.endswith(".py") or not is_test_file(path, candidate):
        return []
    try:
        tree = ast.parse(candidate)
    except SyntaxError:
        return []
    errors = []
    for call in _patch_calls(tree):
        target = _target_string(call)
        if target is None:
            continue
        parts = target.split(".")
        last = parts[-1]
        if last in _REMOVED_MODULE_NAMES and "genai" in parts:
            genai_at = parts.index("genai")
            module = ".".join(parts[: genai_at + 1])
            errors.append(
                f"line {call.lineno}: `patch({target!r})` patches `{last}`, which the new SDK "
                f"does not have (a module-level function became a client method): patch "
                f"`{module}.Client` instead and set its `models`/`chats` return value"
            )
        elif last == "Client" and _autospec(call):
            errors.append(
                f"line {call.lineno}: `patch({target!r}, autospec=True)`: Client's resources "
                "(models, chats, aio, ...) are properties, which autospec does not follow; "
                "drop autospec=True"
            )
    return errors


def rewrite_checker(root: Path, findings: Sequence[Finding]):  # noqa: ARG001
    def check(path: str, original: str, candidate: str) -> list[str]:  # noqa: ARG001
        return _patch_errors(path, candidate)

    return check
