"""Changes a whole-file rewrite makes that the migration does not need (Python files).

The candidate is compared with the original line by line (the same diff as
`diffing.changed_lines`). Each changed region (hunk) is then judged:

- import hunks are kept;
- text-only hunks (the token streams match once comments, blank lines, whitespace and string
  contents are set aside) are kept when they touch a string on a finding's line, or only change
  comments/whitespace near a finding's statement; any other is reverted to the original;
- code hunks near a finding's statement, or that use a name the migration's code binds or
  introduces or an attribute it accesses, are kept; any other is an error (sent back through
  the check retry).

After the reverts, every string constant of the original must still be in the candidate (as a
multiset over the whole file), except strings on a finding's lines (mock targets, removed
config values), the keys of a 0.x-shaped mock response dict, and keys that became attribute
or keyword names. Pure and stdlib only; the rule and its limits are in CLAUDE.md.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import io
import keyword
import tokenize
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import asdict, dataclass

from apifix.engine.diffing import opcodes
from apifix.engine.models import Finding

PROXIMITY = 3  # lines between a changed/removed hunk and a finding's statement
EXCERPT_CHARS = 80
STRING_CHARS = 60
_NEVER_NAMES = frozenset({"self", "cls"}) | frozenset(dir(builtins)) | frozenset(keyword.kwlist)
_COMPOUND = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.ExceptHandler,
)
_STRING_TOKENS = {tokenize.STRING} | (
    {tokenize.FSTRING_MIDDLE} if hasattr(tokenize, "FSTRING_MIDDLE") else set()
)
_LAYOUT_TOKENS = {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.ENDMARKER}


@dataclass(frozen=True)
class Hunk:
    """One changed region: original lines [i1, i2) and candidate lines [j1, j2), 0-based."""

    i1: int
    i2: int
    j1: int
    j2: int

    @property
    def insertion(self) -> bool:
        return self.i1 == self.i2


@dataclass(frozen=True)
class Revert:
    """A text-only hunk apifix put back. `line`: the original's first line (or the line an
    insertion followed); `fixed_line`: where the restored text is in the file apifix writes."""

    line: int
    fixed_line: int
    kind: str  # "changed", "added" or "removed"
    old: str  # excerpt of the original's text
    new: str  # excerpt of the model's text

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def hunks(old: str, new: str) -> list[Hunk]:
    return [Hunk(*op) for op in opcodes(old, new)]


# --- facts about one version of the file ------------------------------------------------------


@dataclass(frozen=True)
class _Tok:
    type: int
    string: str
    start: tuple[int, int]
    end_row: int


class _Version:
    """One version of the file: its lines, tokens and AST."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines()
        self.tree = ast.parse(text)
        self.tokens = [
            _Tok(t.type, t.string, t.start, t.end[0])
            for t in tokenize.generate_tokens(io.StringIO(text).readline)
        ]
        self.import_lines: set[int] = set()  # 1-based
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                self.import_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    def tokens_in(self, first: int, last: int) -> list[_Tok]:
        """Tokens overlapping 1-based lines first..last (none when last < first)."""
        if last < first:
            return []
        return [t for t in self.tokens if t.start[0] <= last and t.end_row >= first]

    def nodes_in(self, first: int, last: int) -> list[ast.AST]:
        if last < first:
            return []
        return [
            n
            for n in ast.walk(self.tree)
            if getattr(n, "lineno", None) is not None and first <= n.lineno <= last  # type: ignore[attr-defined]
        ]


def _token_key(t: _Tok) -> tuple[int, str] | None:
    if t.type in (tokenize.COMMENT, tokenize.NL):
        return None
    if t.type in _STRING_TOKENS:
        return (tokenize.STRING, "")
    if t.type in _LAYOUT_TOKENS:
        return (t.type, "")
    return (t.type, t.string)


def _keys(tokens: Iterable[_Tok]) -> list[tuple[int, str]]:
    return [k for t in tokens if (k := _token_key(t)) is not None]


def _strings(tokens: Iterable[_Tok]) -> list[str]:
    return [t.string for t in tokens if t.type in _STRING_TOKENS]


def _comments(tokens: Iterable[_Tok]) -> list[str]:
    return [t.string for t in tokens if t.type == tokenize.COMMENT]


# --- regions and exempt strings ---------------------------------------------------------------


def _start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(d.lineno for d in decorators)])  # type: ignore[attr-defined]


def _span(node: ast.AST) -> tuple[int, int]:
    """A statement's region: simple statements whole, compound ones by their header."""
    start = _start(node)
    end = node.end_lineno or node.lineno  # type: ignore[attr-defined]
    if isinstance(node, _COMPOUND):
        body = node.body  # type: ignore[attr-defined]
        end = max(start, body[0].lineno - 1) if body else end
    return start, end


def _statements(tree: ast.Module) -> list[tuple[int, ast.AST]]:
    """(depth, node) for every statement and except handler."""
    out: list[tuple[int, ast.AST]] = []

    def walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            is_stmt = isinstance(child, ast.stmt | ast.ExceptHandler)
            if is_stmt:
                out.append((depth, child))
            walk(child, depth + 1 if is_stmt else depth)

    walk(tree, 0)
    return out


def regions(tree: ast.Module, lines: Iterable[int]) -> list[tuple[int, int]]:
    """The region of each line: the innermost statement holding it (see _span)."""
    statements = _statements(tree)
    out = set()
    for line in lines:
        holding = [
            (depth, node)
            for depth, node in statements
            if _start(node) <= line <= (node.end_lineno or node.lineno)  # type: ignore[attr-defined]
        ]
        if holding:
            out.add(_span(max(holding, key=lambda dn: dn[0])[1]))
        else:
            out.add((line, line))
    return sorted(out)


def _char_col(line: str, byte_col: int) -> int:
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))


def _exempt_positions(version: _Version, findings: Sequence[Finding]) -> tuple[set[int], set]:
    """Lines whose strings are exempt (a finding's own lines), and the (row, col) positions of
    exempt dict keys (findings on a dict literal, i.e. a 0.x-shaped mock response: only its keys
    are exempt, its values stay protected)."""
    dicts = {
        (n.lineno, n.col_offset + 1): n for n in ast.walk(version.tree) if isinstance(n, ast.Dict)
    }
    lines: set[int] = set()
    keys: set[tuple[int, int]] = set()
    for f in findings:
        node = dicts.get((f.line, f.col))
        if node is None:
            lines.update(range(f.line, f.end_line + 1))
            continue
        for d in ast.walk(node):
            if isinstance(d, ast.Dict):
                for k in d.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        row = k.lineno
                        keys.add((row, _char_col(version.lines[row - 1], k.col_offset)))
    return lines, keys


def _exempt_token(t: _Tok, lines: set[int], keys: set[tuple[int, int]]) -> bool:
    return t.start[0] in lines or t.start in keys


def _exempt_node(
    node: ast.Constant, version: _Version, lines: set[int], keys: set[tuple[int, int]]
) -> bool:
    if node.lineno in lines:
        return True
    row = node.lineno
    return (row, _char_col(version.lines[row - 1], node.col_offset)) in keys


# --- names ------------------------------------------------------------------------------------


def _bound(nodes: Iterable[ast.AST]) -> set[str]:
    names: set[str] = set()
    for n in nodes:
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store | ast.Del):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(n.name)
        elif isinstance(n, ast.alias):
            names.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
    return names - _NEVER_NAMES


def _used(nodes: Iterable[ast.AST]) -> tuple[set[str], set[str]]:
    """(identifiers, attribute names) in `nodes`."""
    names: set[str] = set()
    attrs: set[str] = set()
    for n in nodes:
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            attrs.add(n.attr)
        elif isinstance(n, ast.Global | ast.Nonlocal):
            names.update(n.names)
    return names | _bound(nodes), attrs


def _instance_attributes(nodes: Iterable[ast.AST]) -> set[str]:
    """`x` of every `self.x` / `cls.x`."""
    return {
        n.attr
        for n in nodes
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id in ("self", "cls")
    }


def _library_names(tree: ast.Module, library_modules: Collection[str]) -> set[str]:
    """The library's module names and the names a file imports from it."""
    names = set(library_modules)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in library_modules:
                    names.add((a.asname or a.name).split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom)
            and not node.level
            and (node.module or "").split(".")[0] in library_modules
        ):
            names.update(a.asname or a.name for a in node.names if a.name != "*")
    return names


# --- the check --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Judged:
    hunk: Hunk
    kind: str  # "import", "text" (text-only) or "code"
    verdict: str  # "keep", "revert" or "reject"


class _Diff:
    def __init__(
        self,
        original: str,
        candidate: str,
        findings: Sequence[Finding],
        library_modules: Collection[str],
    ) -> None:
        self.old, self.new = _Version(original), _Version(candidate)
        self.hunks = hunks(original, candidate)
        lines = {line for f in findings for line in range(f.line, f.end_line + 1)}
        self.regions = regions(self.old.tree, sorted({f.line for f in findings}))
        self.finding_lines = lines
        self.exempt_lines, self.exempt_keys = _exempt_positions(self.old, findings)
        self.library_modules = library_modules
        self.judged = self._judge()

    # one hunk's sides

    def _old_side(self, h: Hunk) -> tuple[int, int]:
        return h.i1 + 1, h.i2

    def _new_side(self, h: Hunk) -> tuple[int, int]:
        return h.j1 + 1, h.j2

    def _near(self, h: Hunk) -> bool:
        if h.insertion:  # strictly inside a region: between two of its lines
            return any(start <= h.i1 < end for start, end in self.regions)
        first, last = self._old_side(h)
        return any(
            first <= end + PROXIMITY and last >= start - PROXIMITY for start, end in self.regions
        )

    def _code_lines(self, version: _Version, first: int, last: int) -> list[int]:
        out = []
        for n in range(first, last + 1):
            s = version.lines[n - 1].strip() if n <= len(version.lines) else ""
            if s and not s.startswith("#"):
                out.append(n)
        return out

    def _import_hunk(self, h: Hunk) -> bool:
        old = self._code_lines(self.old, *self._old_side(h))
        new = self._code_lines(self.new, *self._new_side(h))
        return (
            bool(old or new)
            and all(n in self.old.import_lines for n in old)
            and all(n in self.new.import_lines for n in new)
        )

    def _tokens(self, h: Hunk) -> tuple[list[_Tok], list[_Tok]]:
        return self.old.tokens_in(*self._old_side(h)), self.new.tokens_in(*self._new_side(h))

    def _text_only(self, h: Hunk) -> bool:
        old, new = self._tokens(h)
        return _keys(old) == _keys(new)

    def _touches_exempt(self, h: Hunk) -> bool:
        old, _ = self._tokens(h)
        return any(
            t.type in _STRING_TOKENS and _exempt_token(t, self.exempt_lines, self.exempt_keys)
            for t in old
        )

    def _strings_equal(self, h: Hunk) -> bool:
        old, new = self._tokens(h)
        return _strings(old) == _strings(new)

    def _judge(self) -> list[_Judged]:
        kinds = []
        for h in self.hunks:
            if self._import_hunk(h):
                kinds.append((h, "import"))
            elif self._text_only(h):
                kinds.append((h, "text"))
            else:
                kinds.append((h, "code"))
        # Migration names: the library's; names bound on either side of the code hunks near a
        # finding and on the candidate side of import hunks; names those code hunks introduce
        # (used on the candidate side, nowhere in the original: `client`, also as an instance
        # attribute `self.client`); attributes they access.
        names = _library_names(self.new.tree, self.library_modules)
        attrs: set[str] = set()
        before_names, before_attrs = _used(ast.walk(self.old.tree))
        before = before_names | before_attrs
        for h, kind in kinds:
            if kind == "import":
                names |= _bound(self.new.nodes_in(*self._new_side(h)))
            elif kind == "code" and self._near(h):
                new_nodes = self.new.nodes_in(*self._new_side(h))
                used, accessed = _used(new_nodes)
                names |= _bound(self.old.nodes_in(*self._old_side(h))) | _bound(new_nodes)
                names |= (used | _instance_attributes(new_nodes)) - before
                attrs |= accessed
        names -= _NEVER_NAMES
        judged = []
        for h, kind in kinds:
            if kind == "import":
                verdict = "keep"
            elif kind == "text":
                keep = self._touches_exempt(h) or (self._strings_equal(h) and self._near(h))
                verdict = "keep" if keep else "revert"
            else:
                used_names: set[str] = set()
                used_attrs: set[str] = set()
                for version, side in ((self.old, self._old_side(h)), (self.new, self._new_side(h))):
                    n, a = _used(version.nodes_in(*side))
                    used_names |= n
                    used_attrs |= a
                related = self._near(h) or bool(used_names & names) or bool(used_attrs & attrs)
                verdict = "keep" if related else "reject"
            judged.append(_Judged(h, kind, verdict))
        return judged

    # messages

    def _excerpt(
        self, lines: list[str], first: int, last: int, code: bool = False
    ) -> tuple[int, str]:
        """(line number, text) of the first non-blank line in first..last; with `code`, of
        the first one that is not a comment, if any."""
        found = [
            (n, lines[n - 1].strip())
            for n in range(first, min(last, len(lines)) + 1)
            if lines[n - 1].strip()
        ]
        if code:
            found = [f for f in found if not f[1].startswith("#")] or found
        if not found:
            return first, ""
        n, s = found[0]
        return n, s if len(s) <= EXCERPT_CHARS else s[: EXCERPT_CHARS - 3] + "..."

    def where(self, h: Hunk) -> str:
        if h.insertion:
            return f"after original line {h.i1}" if h.i1 else "before original line 1"
        return f"original line {h.i1 + 1}"

    def code_error(self, h: Hunk) -> str:
        if h.insertion:
            _, text = self._excerpt(self.new.lines, *self._new_side(h), code=True)
            return (
                f'{self.where(h)}: added code unrelated to the migration ("{text}"); remove it, '
                "make only the changes the migration needs"
            )
        what = "removed" if h.j1 == h.j2 else "changed"
        line, text = self._excerpt(self.old.lines, *self._old_side(h), code=True)
        return (
            f'original line {line}: {what} code unrelated to the migration ("{text}"); keep it '
            "exactly as in the original"
        )

    def text_error(self, h: Hunk) -> str:
        if h.insertion:
            _, text = self._excerpt(self.new.lines, *self._new_side(h))
            return (
                f'{self.where(h)}: added text unrelated to the migration ("{text}"); remove it, '
                "make only the changes the migration needs"
            )
        _, text = self._excerpt(self.old.lines, *self._old_side(h))
        return (
            f'{self.where(h)}: changed text unrelated to the migration ("{text}"); keep it '
            "exactly as in the original"
        )

    def revert_record(self, h: Hunk, fixed_line: int) -> Revert:
        old = self.old.lines[h.i1 : h.i2]
        new = self.new.lines[h.j1 : h.j2]
        kind = "added" if not old else "removed" if not new else "changed"
        return Revert(h.i1 + 1 if old else h.i1, fixed_line, kind, *_changed_excerpts(old, new))


def _clip(s: str, limit: int = EXCERPT_CHARS) -> str:
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _changed_excerpts(old: list[str], new: list[str]) -> tuple[str, str]:
    """Short excerpts around the first difference: (original's, model's)."""
    for a, b in zip(old, new, strict=False):
        if a == b:
            continue
        p = 0
        while p < min(len(a), len(b)) and a[p] == b[p]:
            p += 1
        s = 0
        while s < min(len(a), len(b)) - p and a[-1 - s] == b[-1 - s]:
            s += 1
        return _window(a, p, len(a) - s), _window(b, p, len(b) - s)
    first = next((x.strip() for x in old if x.strip()), "")
    added = next((x.strip() for x in new if x.strip()), "")
    return _clip(first), _clip(added)


def _window(line: str, start: int, end: int, context: int = 25) -> str:
    lo, hi = max(0, start - context), min(len(line), max(end, start) + context)
    text = line[lo:hi].strip()
    return ("..." if lo > 0 else "") + text + ("..." if hi < len(line) else "")


def _map_line(before: list[str], after: list[str], line: int) -> int:
    """Where 1-based `line` of `before` is in `after` (both mostly equal)."""
    x = line - 1
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for op, i1, i2, j1, _j2 in matcher.get_opcodes():
        if i1 <= x < i2:
            return j1 + (x - i1 if op == "equal" else 0) + 1
    return line + len(after) - len(before)


@dataclass(frozen=True)
class RevertResult:
    text: str  # the candidate with text-only unrelated hunks put back
    reverted: list[Revert]
    errors: list[str]  # reverts that did not parse, as check errors


def revert_text_edits(
    original: str,
    candidate: str,
    findings: Sequence[Finding],
    library_modules: Collection[str] = (),
) -> RevertResult:
    """Put back text-only hunks unrelated to the migration (strings, docstrings, comments away
    from the findings). If the result does not parse, nothing is reverted and each such hunk is
    an error instead. Both versions must parse; a file that does not tokenize is left as is."""
    try:
        diff = _Diff(original, candidate, findings, library_modules)
    except (SyntaxError, tokenize.TokenError, ValueError):
        return RevertResult(candidate, [], [])
    targets = [j.hunk for j in diff.judged if j.verdict == "revert"]
    if not targets:
        return RevertResult(candidate, [], [])
    old_lines = original.splitlines(keepends=True)
    new_lines = candidate.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in candidate else "\n"
    for h in sorted(targets, key=lambda h: h.j1, reverse=True):
        restored = old_lines[h.i1 : h.i2]
        if restored and not restored[-1].endswith(("\n", "\r")) and h.j2 < len(new_lines):
            restored[-1] += newline  # the original's unterminated last line, now mid-file
        new_lines[h.j1 : h.j2] = restored
    text = "".join(new_lines)
    if text and not text.endswith(("\n", "\r")) and candidate.endswith(("\n", "\r")):
        text += newline
    if not _parses(text):
        return RevertResult(candidate, [], [diff.text_error(h) for h in targets])
    reverted = []
    shift = 0
    for h in sorted(targets, key=lambda h: h.j1):
        reverted.append(diff.revert_record(h, h.j1 + 1 + shift))
        shift += (h.i2 - h.i1) - (h.j2 - h.j1)
    return RevertResult(text, reverted, [])


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
    except SyntaxError:
        return False
    return True


def relocate(reverted: Sequence[Revert], before: str, after: str) -> list[Revert]:
    """The reverts with `fixed_line` mapped from `before` (the reverted text) into `after` (the
    text apifix writes, e.g. with imports added)."""
    if before == after:
        return list(reverted)
    a, b = before.splitlines(), after.splitlines()
    return [Revert(r.line, _map_line(a, b, r.fixed_line), r.kind, r.old, r.new) for r in reverted]


def unrelated_edits(
    original: str,
    candidate: str,
    findings: Sequence[Finding],
    library_modules: Collection[str] = (),
) -> list[str]:
    """Check errors for code hunks unrelated to the migration and for original strings that
    are missing (see the module docstring). Text-only hunks are revert_text_edits' job; any
    still left are not reported here."""
    try:
        diff = _Diff(original, candidate, findings, library_modules)
    except (SyntaxError, tokenize.TokenError, ValueError):
        return []
    rejected = [j.hunk for j in diff.judged if j.verdict == "reject"]
    reverts = [j.hunk for j in diff.judged if j.verdict == "revert"]  # left after a failed revert
    errors = [diff.code_error(h) for h in rejected]
    errors += _missing_strings(diff, rejected + reverts)
    return errors


def _string_constants(tree: ast.Module) -> list[ast.Constant]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _key_constants(tree: ast.Module) -> set[int]:
    """ids of the string constants used as dict keys or subscript keys."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            out.update(id(k) for k in node.keys if isinstance(k, ast.Constant))
        elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            out.add(id(node.slice))
    return out


def _attribute_names(tree: ast.Module) -> set[str]:
    """Attribute and keyword-argument names: where a dict key goes when a dict becomes an
    object (`r["choices"]` -> `r.choices`, `{"content": x}` -> `SimpleNamespace(content=x)`)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            out.add(node.arg)
    return out


def _missing_strings(diff: _Diff, skipped: Sequence[Hunk]) -> list[str]:
    """Non-exempt string constants of the original that the candidate lacks (multiset). Exempt
    besides those on finding lines: keys that became attribute or keyword names. Strings in the
    `skipped` hunks (rejected or text-only) are reported by those hunks' own errors."""
    have = Counter(n.value for n in _string_constants(diff.new.tree))
    keys, attributes = _key_constants(diff.old.tree), _attribute_names(diff.new.tree)
    nodes = [
        n
        for n in _string_constants(diff.old.tree)
        if not _exempt_node(n, diff.old, diff.exempt_lines, diff.exempt_keys)
        and not (id(n) in keys and n.value in attributes)
    ]
    need = Counter(n.value for n in nodes)
    changed = {line for h in diff.hunks for line in range(h.i1 + 1, h.i2 + 1)}
    in_rejected = {line for h in skipped for line in range(h.i1 + 1, h.i2 + 1)}
    errors: list[tuple[int, str]] = []
    for value, count in need.items():
        missing = count - have.get(value, 0)
        if missing <= 0:
            continue
        where = sorted(
            (n for n in nodes if n.value == value),
            key=lambda n: (_lines_of(n).isdisjoint(changed), n.lineno),
        )[:missing]
        for n in where:
            if not _lines_of(n).isdisjoint(in_rejected):
                continue
            shown = (
                repr(value) if len(value) <= STRING_CHARS else repr(value[:STRING_CHARS]) + "..."
            )
            message = (
                f"original line {n.lineno}: the string {shown} is missing; keep string literals "
                "exactly as in the original unless they name the old API"
            )
            errors.append((n.lineno, message))
    return [message for _, message in sorted(errors)]


def _lines_of(node: ast.AST) -> set[int]:
    return set(range(node.lineno, (node.end_lineno or node.lineno) + 1))  # type: ignore[attr-defined]
