"""Migration-specific rewrite checks: `key`, `settings`, `safety` (Migration.rewrite_checker).

Pure `ast`, built once per job from the original repo. Each check is intentionally a
substring/presence heuristic rather than full dataflow (matching openai_v1's precision would be
a much larger undertaking for a first version of this migration); known gaps are in
docs/notes.md.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from apifix.engine.models import Finding

RewriteCheck = Callable[[str, str, str], list[str]]

_SETTINGS_KEYWORDS = (
    "system_instruction",
    "temperature",
    "top_p",
    "top_k",
    "candidate_count",
    "max_output_tokens",
    "stop_sequences",
    "response_mime_type",
    "response_schema",
    "tools",
    "tool_config",
)
_SAFETY_CATEGORY_RE = re.compile(r"\bHARM_CATEGORY_[A-Z_]+\b")
_SAFETY_THRESHOLD_RE = re.compile(r"\bBLOCK_[A-Z_]+\b|\bHARM_BLOCK_THRESHOLD_[A-Z_]+\b|\bOFF\b")
_SHORTHAND_CATEGORY = {
    "harassment": "HARM_CATEGORY_HARASSMENT",
    "hate": "HARM_CATEGORY_HATE_SPEECH",
    "hate_speech": "HARM_CATEGORY_HATE_SPEECH",
    "sexual": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "sexually_explicit": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "dangerous": "HARM_CATEGORY_DANGEROUS_CONTENT",
    "dangerous_content": "HARM_CATEGORY_DANGEROUS_CONTENT",
}
_SHORTHAND_THRESHOLD = {
    "block_none": "BLOCK_NONE",
    "block_only_high": "BLOCK_ONLY_HIGH",
    "block_low_and_above": "BLOCK_LOW_AND_ABOVE",
    "block_medium_and_above": "BLOCK_MEDIUM_AND_ABOVE",
}


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _calls(tree: ast.Module, name: str) -> list[ast.Call]:
    """Calls whose function is named `name` (by its last dotted segment) anywhere in `tree`."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if attr == name:
                out.append(node)
    return out


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _source(node: ast.AST, text: str) -> str:
    return " ".join((ast.get_source_segment(text, node) or ast.unparse(node)).split())


def _carries(
    value: ast.expr, expr: str, assigned: dict[str, list[ast.expr]], seen: set[str]
) -> bool:
    """True when `value` is, or has among its operands (`or`, a conditional, a call's
    arguments...), the key expression `expr` (an `ast.dump`), or a local name assigned from
    something that does."""
    for node in ast.walk(value):
        if isinstance(node, ast.expr) and ast.dump(node) == expr:
            return True
        if isinstance(node, ast.Name) and node.id not in seen:
            seen.add(node.id)
            if any(_carries(v, expr, assigned, seen) for v in assigned.get(node.id, [])):
                return True
    return False


def _assignments(tree: ast.Module) -> dict[str, list[ast.expr]]:
    out: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name):
            targets = [node.target]
        else:
            continue
        value = getattr(node, "value", None)
        if value is not None:
            for t in targets:
                out.setdefault(t.id, []).append(value)
    return out


def _env_read(node: ast.AST) -> str | None:
    """The environment variable a node reads (`os.getenv("X")`, `os.environ.get("X")`,
    `os.environ["X"]`), or None."""
    if isinstance(node, ast.Call) and node.args:
        func, arg = node.func, node.args[0]
        reads = isinstance(func, ast.Attribute) and (
            func.attr == "getenv" or (func.attr == "get" and _name(func.value) == "environ")
        )
        if reads and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    if (
        isinstance(node, ast.Subscript)
        and _name(node.value) == "environ"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _env_reads(
    value: ast.expr, assigned: dict[str, list[ast.expr]], seen: set[str]
) -> dict[str, ast.AST]:
    """Environment variables `value` reads (following local names), each with its read."""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(value):
        if (env := _env_read(node)) is not None:
            out.setdefault(env, node)
        elif isinstance(node, ast.Name) and node.id not in seen:
            seen.add(node.id)
            for v in assigned.get(node.id, []):
                for env, read in _env_reads(v, assigned, seen).items():
                    out.setdefault(env, read)
    return out


def _leaves(
    value: ast.expr, assigned: dict[str, list[ast.expr]], seen: set[str], keep: str
) -> list[ast.expr]:
    """The values `value` can take, in the order they are tried: the operands of `or`, the two
    arms of a conditional (not its test), the values a local name was assigned (except the
    expression `keep`, an `ast.dump`, which stays whole)."""
    if isinstance(value, ast.BoolOp):
        return [leaf for v in value.values for leaf in _leaves(v, assigned, seen, keep)]
    if isinstance(value, ast.IfExp):
        return _leaves(value.body, assigned, seen, keep) + _leaves(
            value.orelse, assigned, seen, keep
        )
    if (
        isinstance(value, ast.Name)
        and value.id not in seen
        and assigned.get(value.id)
        and ast.dump(value) != keep
    ):
        seen.add(value.id)
        return [leaf for v in assigned[value.id] for leaf in _leaves(v, assigned, seen, keep)]
    return [value]


def _key_source_error(
    line: int, text: str, original_reads: dict[str, ast.AST], added: list[str], source: str
) -> str:
    read = _source(next(iter(original_reads.values())), source)
    names = ", ".join(added)
    return (
        f"original line {line}: the original reads the key from `{read}`; keep that exactly, "
        f"don't add {names} (a different or earlier variable can win over the original). Pass "
        f"the same value to `genai.Client(api_key={text})`"
    )


def _key_errors(original: str, candidate: str) -> list[str]:
    old = _parse(original)
    new = _parse(candidate)
    if old is None or new is None:
        return []
    errors = []
    assigned = _assignments(new)
    old_assigned = _assignments(old)
    for call in _calls(old, "configure"):
        key = _kwarg(call, "api_key")
        if key is None:
            continue  # implicit (reads the environment); not enforced here
        expr = ast.dump(key)
        text = _source(key, original)
        original_reads = _env_reads(key, old_assigned, set())
        values = [v for c in _calls(new, "Client") if (v := _kwarg(c, "api_key")) is not None]
        carriers = [v for v in values if _carries(v, expr, assigned, set())]
        if not carriers:
            added = sorted(
                {e for v in values for e in _env_reads(v, assigned, set())} - set(original_reads)
            )
            if original_reads and added:
                errors.append(_key_source_error(call.lineno, text, original_reads, added, original))
                continue
            errors.append(
                f"original line {call.lineno}: `configure(api_key={text}, ...)`'s key does not "
                f"reach a `genai.Client(api_key={text})` in the rewrite: never drop a key value "
                "(the new client raises with no key and none set in the environment)"
            )
            continue
        # `original or fallback` keeps the original winning; a fallback tried first does not
        for v in carriers:
            leaves = _leaves(v, assigned, set(), expr)
            first = next(
                (i for i, leaf in enumerate(leaves) if _carries(leaf, expr, assigned, set())),
                None,
            )
            before = {
                e
                for leaf in leaves[: first or 0]
                for e in _env_reads(leaf, assigned, set())
                if e not in original_reads
            }
            if before:
                errors.append(
                    _key_source_error(call.lineno, text, original_reads, sorted(before), original)
                    if original_reads
                    else f"original line {call.lineno}: the rewrite tries "
                    f"{', '.join(sorted(before))} before the original key `{text}`, so it can win "
                    f"over it; keep the original first: `{text} or <fallback>`"
                )
                break
    return errors


def _generation_config_keywords(node: ast.expr | None) -> set[str]:
    if not (isinstance(node, ast.Call) and _name(node.func) == "GenerationConfig"):
        return set()
    return {kw.arg for kw in node.keywords if kw.arg}


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", None)


def _settings_errors(original: str, candidate: str) -> list[str]:
    old = _parse(original)
    if old is None:
        return []
    errors = []
    for call in _calls(old, "GenerativeModel"):
        kept = {kw.arg for kw in call.keywords if kw.arg and kw.arg in _SETTINGS_KEYWORDS}
        kept |= _generation_config_keywords(_kwarg(call, "generation_config"))
        missing = [kw for kw in kept if f"{kw}=" not in candidate and f'"{kw}"' not in candidate]
        if missing:
            text = _source(call, original)
            errors.append(
                f"original line {call.lineno}: `{text}` set {', '.join(sorted(missing))}, which "
                "the rewrite drops: move every GenerativeModel/GenerationConfig keyword into the "
                "GenerateContentConfig passed on each call (a dropped setting changes behaviour "
                "silently)"
            )
    return errors


def _safety_tokens(node: ast.expr | None, source: str) -> set[str]:
    if node is None:
        return set()
    text = ast.get_source_segment(source, node) or ast.unparse(node)
    tokens = set(_SAFETY_CATEGORY_RE.findall(text)) | set(_SAFETY_THRESHOLD_RE.findall(text))
    for m in re.findall(r'"(\w+)"|\'(\w+)\'', text):
        word = (m[0] or m[1]).lower()
        if word in _SHORTHAND_CATEGORY:
            tokens.add(_SHORTHAND_CATEGORY[word])
        if word in _SHORTHAND_THRESHOLD:
            tokens.add(_SHORTHAND_THRESHOLD[word])
    return tokens


def _safety_errors(original: str, candidate: str) -> list[str]:
    old = _parse(original)
    if old is None:
        return []
    errors = []
    seen: set[str] = set()
    for call in _calls(old, "GenerativeModel"):
        safety = _kwarg(call, "safety_settings")
        tokens = _safety_tokens(safety, original)
        if not tokens or tokens <= seen:
            continue
        seen |= tokens
        missing = sorted(t for t in tokens if t not in candidate)
        if missing:
            errors.append(
                f"original line {call.lineno}: safety_settings named {', '.join(missing)}, not "
                "found in the rewrite: keep every category and threshold as it was "
                "(types.SafetySetting(category=..., threshold=...))"
            )
    return errors


class _Checker:
    def __init__(self, root: Path, findings: Sequence[Finding]) -> None:
        self.root = root
        self.files = sorted({f.file for f in findings if f.file.endswith(".py")})

    def __call__(self, path: str, original: str, candidate: str) -> dict[str, list[str]]:
        if not path.endswith(".py"):
            return {"key": [], "settings": [], "safety": []}
        return {
            "key": _key_errors(original, candidate),
            "settings": _settings_errors(original, candidate),
            "safety": _safety_errors(original, candidate),
        }


def rewrite_checker(root: Path, findings: Sequence[Finding]) -> _Checker:
    return _Checker(root, findings)
