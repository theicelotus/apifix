"""Changes a whole-file rewrite makes that the migration does not need (Python files).

The candidate is compared with the original line by line (the same diff as
`diffing.changed_lines`). Each changed region (hunk) is then judged:

- import hunks are kept;
- text-only hunks (the token streams match once comments, blank lines, whitespace, string
  contents and docstrings are set aside) are kept when they touch a string on a finding's line,
  or only change comments/whitespace near a finding's statement; any other is reverted to the
  original (so an added, removed or changed docstring is put back, however the diff splits it);
- code hunks near a finding's statement are kept; so are code hunks elsewhere that use an
  openai-bound name (see _Bound: the library's names and what is assigned from them, in scope)
  or a name a near code hunk binds or introduces, in the same scope, and hunks whose new side
  names one of the library's environment variables (`env_names`: a test setting a placeholder
  `OPENAI_API_KEY` for the real client); any other is an error
  (sent back through the check retry). Attribute names alone (`.chat`, `.choices`, which other
  SDKs share) never make a hunk related;
- with `renames` (the migration's keyword renames, given for test files), a hunk whose only
  change is one of them, as a keyword argument or as a string (`engine=` -> `model=` in
  `create.assert_called_once_with(...)`, `call_args.kwargs["engine"]`), is kept wherever it is:
  the test's expectation of the call follows the migrated call.

After the reverts, every string constant of the original must still be in the candidate (as a
multiset over the whole file), except strings on a finding's lines (mock targets, removed
config values), the keys of a 0.x-shaped mock response dict, keys that became attribute
or keyword names, and the strings a kept rename hunk renamed. Pure and stdlib only; the rule
and its limits are in docs/notes.md.
"""

from __future__ import annotations

import ast
import builtins
import difflib
import io
import keyword
import tokenize
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
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
    doc: bool = False  # part of a docstring statement (see _docstring_tokens)


def _docstrings(tree: ast.Module) -> list[ast.Expr]:
    """The docstring statements: a string as the first statement of the module, a class or a
    function."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.append(first)
    return out


def _docstring_tokens(
    tokens: list[tokenize.TokenInfo], lines: list[str], docs: list[ast.Expr]
) -> set[int]:
    """Indexes of the tokens that make up docstring statements: their strings, the NEWLINE
    ending them, and the INDENT on their first line (it moves to the next statement's line
    when a docstring is added or removed)."""
    spans = []
    for d in docs:
        start = (d.lineno, _char_col(lines[d.lineno - 1], d.col_offset))
        end_row = d.end_lineno or d.lineno
        end = (end_row, _char_col(lines[end_row - 1], d.end_col_offset or 0))
        spans.append((start, end))
    out: set[int] = set()
    for start, end in spans:
        inside = [i for i, t in enumerate(tokens) if start <= t.start and t.end <= end]
        if not inside:
            continue
        out.update(inside)
        after = inside[-1] + 1
        if after < len(tokens) and tokens[after].type == tokenize.NEWLINE:
            out.add(after)
        before = inside[0] - 1
        if (
            before >= 0
            and tokens[before].type == tokenize.INDENT
            and tokens[before].start[0] == start[0]
        ):
            out.add(before)
    return out


class _Version:
    """One version of the file: its lines, tokens and AST."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines()
        self.tree = ast.parse(text)
        raw = list(tokenize.generate_tokens(io.StringIO(text).readline))
        docs = _docstrings(self.tree)
        doc = _docstring_tokens(raw, self.lines, docs)
        self.tokens = [
            _Tok(t.type, t.string, t.start, t.end[0], i in doc) for i, t in enumerate(raw)
        ]
        # 1-based lines holding only docstring text (not a `def f(): """doc"""` line)
        self.docstring_lines: set[int] = set()
        for d in docs:
            first, last = d.lineno, d.end_lineno or d.lineno
            if self.lines[first - 1][: _char_col(self.lines[first - 1], d.col_offset)].isspace():
                self.docstring_lines.update(range(first, last + 1))
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
    if t.type in (tokenize.COMMENT, tokenize.NL) or t.doc:
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


def _string_value(t: _Tok) -> str | None:
    """A plain string token's value (None for f-strings, bytes and the like)."""
    try:
        value = ast.literal_eval(t.string)
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _exact_keys(
    tokens: Iterable[_Tok], renames: Mapping[str, str] | None = None, keywords: Collection = ()
) -> list[tuple[int, str]]:
    """_keys with string contents compared too; with `renames`, keyword-argument names at
    `keywords` ((row, col) positions) and plain strings equal to a rename's old name become
    the new name."""
    out = []
    for t in tokens:
        key = _token_key(t)
        if key is None:
            continue
        if t.type in _STRING_TOKENS:
            value = _string_value(t)
            if renames and value in renames:
                key = (tokenize.STRING, repr(renames[value]))
            else:
                key = (tokenize.STRING, repr(value) if value is not None else t.string)
        elif renames and t.type == tokenize.NAME and t.start in keywords:
            key = (tokenize.NAME, renames[t.string])
        out.append(key)
    return out


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


# --- scopes and openai-bound names -------------------------------------------------------------

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_PATCH_CALLS = frozenset({"patch", "mock.patch", "unittest.mock.patch", "mocker.patch"})
_PATCH_OBJECT_CALLS = frozenset(f"{p}.object" for p in _PATCH_CALLS)
_PATCH_VALUES = ("new", "return_value", "side_effect", "wraps")  # keywords naming the stand-in


def _qual(scope: str, name: str) -> str:
    return f"{scope}.{name}" if scope else name


def _enclosing(scope: str) -> list[str]:
    """`scope` and the scopes around it, innermost first: "C.m" -> ["C.m", "C", ""]."""
    out = [scope]
    while scope:
        scope = scope.rpartition(".")[0]
        out.append(scope)
    return out


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _self_attr(node: ast.AST) -> str | None:
    """`x` of `self.x` / `cls.x`."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    ):
        return node.attr
    return None


class _Scopes:
    """Each node's scope: the qualified name of the innermost def or class around it ("" for
    the module, "C.m" for a method). A def's name, decorators and a class's bases belong to
    the scope around it."""

    def __init__(self, tree: ast.Module) -> None:
        self.of: dict[int, str] = {}
        self.cls: dict[str, str] = {}  # function scope -> the class scope its self/cls sees
        self.defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self.methods: set[str] = set()
        stack: list[tuple[ast.AST, str, str, bool]] = [(tree, "", "", False)]
        while stack:
            node, scope, cls, in_class = stack.pop()
            self.of[id(node)] = scope
            outer: list[ast.AST]
            if isinstance(node, ast.ClassDef):
                inner = _qual(scope, node.name)
                outer = [*node.decorator_list, *node.bases, *node.keywords]
                stack += [(n, scope, cls, in_class) for n in outer]
                stack += [(n, inner, inner, True) for n in node.body]
            elif isinstance(node, _DEFS):
                inner = _qual(scope, node.name)
                self.defs[inner] = node
                self.cls[inner] = cls
                if in_class:
                    self.methods.add(inner)
                outer = [*node.decorator_list, *([node.returns] if node.returns else [])]
                stack += [(n, scope, cls, in_class) for n in outer]
                stack += [(n, inner, cls, False) for n in [node.args, *node.body]]
            else:
                stack += [(n, scope, cls, in_class) for n in ast.iter_child_nodes(node)]

    def scope(self, node: ast.AST) -> str:
        return self.of.get(id(node), "")

    def class_of(self, scope: str) -> str:
        return self.cls.get(scope, "")


def _own_nodes(fn: ast.AST) -> Iterable[ast.AST]:
    """The nodes of a function's body, without nested defs, classes and lambdas."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_DEFS, ast.ClassDef, ast.Lambda)):
            stack += ast.iter_child_nodes(node)


class _Bound:
    """The openai-bound names of one version of the file, by scope: the library's module names
    and the names imported from it; names (and `self.x`) assigned from an expression rooted at
    one (`client = OpenAI()`, `r = client.chat.completions.create(...)`, `for c in r.choices`,
    `with patch("openai.X") as m`); the stand-ins a library patch is given (`new`,
    `return_value`, ...) and the parameters `@patch("openai.X")` decorators fill; functions
    that return or yield a bound object or call (`get_client()`); and the parameters of a
    function the file calls with a bound name or call (`first_choice(response)`). Computed to a
    fixed point, flow-insensitive."""

    MAX_ROUNDS = 20

    def __init__(self, tree: ast.Module, scopes: _Scopes, library_modules: Collection[str]):
        self.scopes, self.library = scopes, frozenset(library_modules)
        self.names: set[tuple[str, str]] = {("", n) for n in _library_names(tree, library_modules)}
        self.attrs: set[tuple[str, str]] = set()  # (class scope, x) of self.x
        nodes = list(ast.walk(tree))
        for _ in range(self.MAX_ROUNDS):
            if not any([self._step(node) for node in nodes]):
                break

    # lookups

    def name(self, name: str, scope: str) -> bool:
        return any((s, name) in self.names for s in _enclosing(scope))

    def attr(self, attr: str, scope: str) -> bool:
        return (self.scopes.class_of(scope), attr) in self.attrs

    def library_patch(self, node: ast.AST) -> bool:
        """`patch("openai.X")` (any patch function) or `patch.object(<bound>, ...)`."""
        if not isinstance(node, ast.Call) or not node.args:
            return False
        func, first = _dotted(node.func), node.args[0]
        if func in _PATCH_CALLS:
            return (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.split(".")[0] in self.library
            )
        return func in _PATCH_OBJECT_CALLS and self.rooted(first, self.scopes.scope(node))

    def refers(self, node: ast.AST) -> bool:
        """Does this node itself name something bound (a name, `self.x`, a library patch)?"""
        scope = self.scopes.scope(node)
        if isinstance(node, ast.Name):
            return self.name(node.id, scope)
        if isinstance(node, ast.arg):
            return self.name(node.arg, scope)
        attr = _self_attr(node)
        if attr is not None:
            return self.attr(attr, scope)
        return self.library_patch(node)

    def rooted(self, expr: ast.AST, scope: str, objects_only: bool = False) -> bool:
        """Is `expr` rooted at a bound name: a name, attribute, subscript or call chain on one
        (`client.chat.completions.create(...)`, `r.choices[0]`), through await, `a or b` and
        `a if c else b`. With `objects_only`, attribute and subscript access that does not end
        in a call is not enough (`r.choices[0].message.content` is data, not a client or a
        response)."""
        if isinstance(expr, ast.Await):
            return self.rooted(expr.value, scope, objects_only)
        if isinstance(expr, ast.BoolOp):
            return any(self.rooted(v, scope, objects_only) for v in expr.values)
        if isinstance(expr, ast.IfExp):
            return self.rooted(expr.body, scope, objects_only) or self.rooted(
                expr.orelse, scope, objects_only
            )
        if (
            objects_only
            and isinstance(expr, ast.Attribute | ast.Subscript)
            and _self_attr(expr) is None
        ):
            return False
        node: ast.AST = expr
        while True:
            if self.library_patch(node):
                return True
            attr = _self_attr(node)
            if attr is not None:
                return self.attr(attr, scope)
            if isinstance(node, ast.Name):
                return self.name(node.id, scope)
            if isinstance(node, ast.Attribute | ast.Subscript):
                node = node.value
            elif isinstance(node, ast.Call):
                node = node.func
            else:
                return False

    # propagation

    def _bind(self, target: ast.AST, scope: str) -> bool:
        before = len(self.names) + len(self.attrs)
        if isinstance(target, ast.Name):
            self.names.add((scope, target.id))
        elif isinstance(target, ast.Tuple | ast.List):
            for elt in target.elts:
                self._bind(elt, scope)
        elif isinstance(target, ast.Starred):
            self._bind(target.value, scope)
        elif (attr := _self_attr(target)) is not None:
            self.attrs.add((self.scopes.class_of(scope), attr))
        return len(self.names) + len(self.attrs) > before

    def _step(self, node: ast.AST) -> bool:
        scope = self.scopes.scope(node)
        changed = False
        if isinstance(node, ast.Assign):
            if self.rooted(node.value, scope):
                for t in node.targets:
                    changed |= self._bind(t, scope)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            if node.value is not None and self.rooted(node.value, scope):
                changed |= self._bind(node.target, scope)
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if item.optional_vars is not None and self.rooted(item.context_expr, scope):
                    changed |= self._bind(item.optional_vars, scope)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            if self.rooted(node.iter, scope):
                changed |= self._bind(node.target, scope)
        elif isinstance(node, ast.Call):
            changed |= self._call(node, scope)
        elif isinstance(node, _DEFS):
            changed |= self._def(node, scope)
        return changed

    def _call(self, node: ast.Call, scope: str) -> bool:
        changed = False
        if self.library_patch(node):
            given = node.args[1:2] if _dotted(node.func) in _PATCH_CALLS else node.args[2:3]
            given += [k.value for k in node.keywords if k.arg in _PATCH_VALUES]
            for value in given:
                changed |= self._bind(value, scope)  # only names and self.x bind
        callee = self._callee(node.func, scope)
        if callee is None:
            return changed
        qual, fn = callee
        params = [*fn.args.posonlyargs, *fn.args.args]
        if qual in self.scopes.methods and isinstance(node.func, ast.Attribute):
            params = params[1:]  # self.m(...): the instance fills the first parameter
        named = {a.arg for a in [*params, *fn.args.kwonlyargs]}
        for i, arg in enumerate(node.args):
            if isinstance(arg, ast.Starred):
                break
            if i < len(params) and self.rooted(arg, scope, objects_only=True):
                changed |= self._bind(ast.Name(params[i].arg, ast.Store()), qual)
        for k in node.keywords:
            if k.arg in named and self.rooted(k.value, scope, objects_only=True):
                changed |= self._bind(ast.Name(k.arg, ast.Store()), qual)
        return changed

    def _callee(
        self, func: ast.AST, scope: str
    ) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef] | None:
        """The file's own function a call reaches: `f(...)` (in this scope or one around it) or
        `self.m(...)` (a method of this class)."""
        if isinstance(func, ast.Name):
            for s in _enclosing(scope):
                qual = _qual(s, func.id)
                if qual in self.scopes.defs:
                    return qual, self.scopes.defs[qual]
            return None
        attr = _self_attr(func)
        if attr is not None:
            qual = _qual(self.scopes.class_of(scope), attr)
            if qual in self.scopes.defs:
                return qual, self.scopes.defs[qual]
        return None

    def _def(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, scope: str) -> bool:
        changed = False
        inner = _qual(scope, fn.name)
        returns = any(
            isinstance(n, ast.Return | ast.Yield | ast.YieldFrom)
            and n.value is not None
            and self.rooted(n.value, inner, objects_only=True)
            for n in _own_nodes(fn)
        )
        if returns:
            if inner in self.scopes.methods:
                before = len(self.attrs)
                self.attrs.add((scope, fn.name))
                changed |= len(self.attrs) > before
            else:
                changed |= self._bind(ast.Name(fn.name, ast.Store()), scope)
        # @patch("openai.X") decorators fill the test's parameters, the lowest decorator first
        params = [a.arg for a in [*fn.args.posonlyargs, *fn.args.args]]
        if params and params[0] in ("self", "cls"):
            params = params[1:]
        slot = 0
        for deco in reversed(fn.decorator_list):
            if not isinstance(deco, ast.Call) or not self._injects(deco):
                continue
            if slot < len(params) and self.library_patch(deco):
                changed |= self._bind(ast.Name(params[slot], ast.Store()), inner)
            slot += 1
        return changed

    @staticmethod
    def _injects(deco: ast.Call) -> bool:
        """Does this patch decorator pass the test a mock (no `new` given)?"""
        func = _dotted(deco.func)
        if any(k.arg == "new" for k in deco.keywords):
            return False
        if func in _PATCH_CALLS:
            return len(deco.args) < 2
        return func in _PATCH_OBJECT_CALLS and len(deco.args) < 3


def _named(node: ast.AST) -> list[str]:
    """The names a node uses or binds (not attributes)."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, (*_DEFS, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, ast.Global | ast.Nonlocal):
        return list(node.names)
    return []


def _alias_and_attr(node: ast.AST) -> list[str]:
    if isinstance(node, ast.alias):
        return [(node.asname or node.name).split(".")[0]]
    return [node.attr] if isinstance(node, ast.Attribute) else []


def _binds(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return isinstance(node.ctx, ast.Store | ast.Del)
    return isinstance(node, (ast.arg, ast.ExceptHandler, ast.ClassDef, ast.Global, *_DEFS))


# --- the check --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Judged:
    hunk: Hunk
    kind: str  # "import", "rename" (keyword renames only), "text" (text-only) or "code"
    verdict: str  # "keep", "revert" or "reject"


class _Diff:
    def __init__(
        self,
        original: str,
        candidate: str,
        findings: Sequence[Finding],
        library_modules: Collection[str],
        env_names: Collection[str] = (),
        renames: Mapping[str, str] | None = None,
    ) -> None:
        self.old, self.new = _Version(original), _Version(candidate)
        self.env_names = frozenset(env_names)
        self.renames = dict(renames or {})
        self.rename_keywords = {
            (n.lineno, _char_col(self.old.lines[n.lineno - 1], n.col_offset))
            for n in ast.walk(self.old.tree)
            if isinstance(n, ast.keyword) and n.arg in self.renames
        }
        self.hunks = hunks(original, candidate)
        lines = {line for f in findings for line in range(f.line, f.end_line + 1)}
        self.regions = regions(self.old.tree, sorted({f.line for f in findings}))
        self.finding_lines = lines
        self.exempt_lines, self.exempt_keys = _exempt_positions(self.old, findings)
        self.library_modules = library_modules
        self.old_scopes, self.new_scopes = _Scopes(self.old.tree), _Scopes(self.new.tree)
        self.old_bound = _Bound(self.old.tree, self.old_scopes, library_modules)
        self.new_bound = _Bound(self.new.tree, self.new_scopes, library_modules)
        self.judged = self._judge()
        # original lines of the kept rename hunks: their renamed strings are not missing
        self.renamed_lines = {
            line
            for j in self.judged
            if j.kind == "rename"
            for line in range(j.hunk.i1 + 1, j.hunk.i2 + 1)
        }

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

    def _rename_only(self, h: Hunk) -> bool:
        """Is the hunk's only change one of the migration's keyword renames (as a keyword
        argument or a string, `engine=` -> `model=`, `kwargs["engine"]` -> `kwargs["model"]`),
        comments and layout aside?"""
        if not self.renames or h.insertion or h.j1 == h.j2:
            return False
        old, new = self._tokens(h)
        renamed = _exact_keys(old, self.renames, self.rename_keywords)
        return renamed == _exact_keys(new) and renamed != _exact_keys(old)

    def _strings_equal(self, h: Hunk) -> bool:
        old, new = self._tokens(h)
        return _strings(old) == _strings(new)

    def _sides(self, h: Hunk) -> list[tuple[list[ast.AST], _Scopes, _Bound]]:
        """Each side's nodes, with that version's scopes and openai-bound names."""
        return [
            (self.old.nodes_in(*self._old_side(h)), self.old_scopes, self.old_bound),
            (self.new.nodes_in(*self._new_side(h)), self.new_scopes, self.new_bound),
        ]

    def _judge(self) -> list[_Judged]:
        kinds = []
        for h in self.hunks:
            if self._import_hunk(h):
                kinds.append((h, "import"))
            elif self._rename_only(h):
                kinds.append((h, "rename"))
            elif self._text_only(h):
                kinds.append((h, "text"))
            else:
                kinds.append((h, "code"))
        # Names of the code hunks near a finding, by scope: what they bind on either side, and
        # what they introduce (used on the candidate side, nowhere in the original: `client`,
        # also as an instance attribute `self.client`, kept per class). A name counts only in
        # its own scope and the scopes inside it, so a `mock_client` or `response` bound near a
        # finding says nothing about another function's variable of the same name. Imports do
        # not count here: the library's are openai-bound, and others (SimpleNamespace) are
        # shared by everything.
        before = {
            name for n in ast.walk(self.old.tree) for name in [*_named(n), *_alias_and_attr(n)]
        }
        scoped: set[tuple[str, str]] = set()
        scoped_attrs: set[tuple[str, str]] = set()
        for h, kind in kinds:
            if kind != "code" or not self._near(h):
                continue
            for i, (nodes, scopes, _) in enumerate(self._sides(h)):
                for n in nodes:
                    scope = scopes.scope(n)
                    for name in _named(n):
                        if _binds(n) or (i == 1 and name not in before):
                            scoped.add((scope, name))
                    attr = _self_attr(n)
                    if i == 1 and attr is not None and attr not in before:
                        scoped_attrs.add((scopes.class_of(scope), attr))
        judged = []
        for h, kind in kinds:
            if kind in ("import", "rename"):
                verdict = "keep"
            elif kind == "text":
                keep = self._touches_exempt(h) or (self._strings_equal(h) and self._near(h))
                verdict = "keep" if keep else "revert"
            else:
                related = (
                    self._near(h)
                    or self._names_library_env(h)
                    or self._uses_migration_names(h, scoped, scoped_attrs)
                )
                verdict = "keep" if related else "reject"
            judged.append(_Judged(h, kind, verdict))
        return judged

    def _names_library_env(self, h: Hunk) -> bool:
        """Does the hunk's new side name one of the library's environment variables (a string
        constant), e.g. `monkeypatch.setenv("OPENAI_API_KEY", "test")`?"""
        return any(
            isinstance(n, ast.Constant) and n.value in self.env_names
            for n in self.new.nodes_in(*self._new_side(h))
        )

    def _uses_migration_names(
        self, h: Hunk, scoped: set[tuple[str, str]], scoped_attrs: set[tuple[str, str]]
    ) -> bool:
        """Does a hunk away from the findings use an openai-bound name, or a name a near code
        hunk binds or introduces, in its scope (an introduced `self.x`: `self.x` or the name
        `x` in a method of the same class)? Attribute names alone never count."""
        for nodes, scopes, bound in self._sides(h):
            for n in nodes:
                if bound.refers(n):
                    return True
                scope = scopes.scope(n)
                cls = scopes.class_of(scope)
                for name in _named(n):
                    if name in _NEVER_NAMES:
                        continue
                    # a near hunk's name in this scope or one around it; or its new `self.x`
                    # seen as a name in a method of that class (a parameter threaded into it)
                    if any((s, name) in scoped for s in _enclosing(scope)) or (
                        cls and (cls, name) in scoped_attrs
                    ):
                        return True
                attr = _self_attr(n)
                if attr is not None and (scopes.class_of(scope), attr) in scoped_attrs:
                    return True
        return False

    # messages

    def _excerpt(
        self, version: _Version, first: int, last: int, code: bool = False
    ) -> tuple[int, str]:
        """(line number, text) of the first non-blank line in first..last; with `code`, of
        the first one that is neither a comment nor docstring text, if any."""
        lines = version.lines
        found = [
            (n, lines[n - 1].strip())
            for n in range(first, min(last, len(lines)) + 1)
            if lines[n - 1].strip()
        ]
        if code:
            found = [
                f for f in found if not f[1].startswith("#") and f[0] not in version.docstring_lines
            ] or found
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
            _, text = self._excerpt(self.new, *self._new_side(h), code=True)
            return (
                f'{self.where(h)}: added code unrelated to the migration ("{text}"); remove it, '
                "make only the changes the migration needs"
            )
        what = "removed" if h.j1 == h.j2 else "changed"
        line, text = self._excerpt(self.old, *self._old_side(h), code=True)
        return (
            f'original line {line}: {what} code unrelated to the migration ("{text}"); keep it '
            "exactly as in the original"
        )

    def text_error(self, h: Hunk) -> str:
        if h.insertion:
            _, text = self._excerpt(self.new, *self._new_side(h))
            return (
                f'{self.where(h)}: added text unrelated to the migration ("{text}"); remove it, '
                "make only the changes the migration needs"
            )
        _, text = self._excerpt(self.old, *self._old_side(h))
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
    return _clip(_first_text(old)), _clip(_first_text(new))


def _first_text(lines: list[str]) -> str:
    """The first non-blank line, stripped, past lines that hold only quotes (the opening line
    of a docstring)."""
    stripped = [x.strip() for x in lines if x.strip()]
    return next((x for x in stripped if x.strip("\"'")), stripped[0] if stripped else "")


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
    env_names: Collection[str] = (),
    renames: Mapping[str, str] | None = None,
) -> RevertResult:
    """Put back text-only hunks unrelated to the migration (strings, docstrings, comments away
    from the findings). If the result does not parse, nothing is reverted and each such hunk is
    an error instead. Both versions must parse; a file that does not tokenize is left as is.
    `renames`: the migration's keyword renames, for test files (see the module docstring)."""
    try:
        diff = _Diff(original, candidate, findings, library_modules, env_names, renames)
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
    env_names: Collection[str] = (),
    renames: Mapping[str, str] | None = None,
) -> list[str]:
    """Check errors for code hunks unrelated to the migration and for original strings that
    are missing (see the module docstring). Text-only hunks are revert_text_edits' job; any
    still left are not reported here."""
    try:
        diff = _Diff(original, candidate, findings, library_modules, env_names, renames)
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
    besides those on finding lines: keys that became attribute or keyword names, and strings a
    kept rename hunk renamed (`kwargs["engine"]` -> `kwargs["model"]`). Strings in the
    `skipped` hunks (rejected or text-only) are reported by those hunks' own errors."""
    have = Counter(n.value for n in _string_constants(diff.new.tree))
    keys, attributes = _key_constants(diff.old.tree), _attribute_names(diff.new.tree)
    nodes = [
        n
        for n in _string_constants(diff.old.tree)
        if not _exempt_node(n, diff.old, diff.exempt_lines, diff.exempt_keys)
        and not (id(n) in keys and n.value in attributes)
        and not (n.value in diff.renames and not _lines_of(n).isdisjoint(diff.renamed_lines))
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
