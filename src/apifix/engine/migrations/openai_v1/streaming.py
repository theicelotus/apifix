"""Key checks on a streamed chunk's delta: `hasattr(delta, "content")`, `"content" in delta`.

0.x stream deltas were dicts (OpenAIObject) that held `content` only when there was text, so
code checked for the key. In 1.x every delta has `content`, None on the role chunk and the final
one: `hasattr` is always true (the None is yielded) and `in` never is (a pydantic model is no
dict). A delta is recognised by its shape, 0.x or 1.x form: `<x>.choices[i].delta`,
`<x>["choices"][i]["delta"]` (also mixed), a name assigned from one, or `<c>.delta` /
`<c>["delta"]` where `c` was assigned from `<x>.choices[i]` or bound by `for c in <x>.choices`.
Names are per function scope and flow-insensitive. Used by the scanner (0.x code) and the
`streaming` rewrite check (whole-file mode).

`"content" [not] in D` is left alone where D is known to be a dict, since a key check is the
right test there: inside a dict guard (`isinstance(G, dict)`, `Mapping`, `MutableMapping`, a
tuple of those) on D or on the name D is read from by subscripts/attributes (a 1.x rewrite that
keeps a branch for dict-shaped chunks), or when D comes from `json.loads` / `json.load`, from
`to_dict()` (openai leaves out unset fields) or from `model_dump(exclude_unset=True |
exclude_none=True)`. A plain `model_dump()` keeps `content: None`, so it stays flagged, and so
does a TypedDict annotation, which the 0.x code itself carries for its dict chunks.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

_MAX_SOURCE = 80
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_DICT_TYPES = frozenset({"dict", "Mapping", "MutableMapping"})
_EXITS = (ast.Continue, ast.Break, ast.Return, ast.Raise)
_DEPTH = 5  # name -> value steps followed back to a dict


@dataclass(frozen=True)
class ContentCheck:
    node: ast.expr  # the hasattr call or the comparison
    check: str  # its source, e.g. 'hasattr(delta, "content")'
    delta: str  # the delta expression's source, e.g. "delta"

    @property
    def line(self) -> int:
        return self.node.lineno


def _source(node: ast.AST, text: str) -> str:
    segment = " ".join((ast.get_source_segment(text, node) or ast.unparse(node)).split())
    return segment if len(segment) <= _MAX_SOURCE else segment[: _MAX_SOURCE - 3] + "..."


def _key(node: ast.AST) -> str | None:
    """The string key of `x["key"]` or the attribute of `x.key`."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _scopes(tree: ast.Module) -> Iterator[list[ast.AST]]:
    """The nodes of each scope (module, def, class, lambda), without nested scopes' nodes."""
    pending: list[ast.AST] = [tree]
    while pending:
        scope = pending.pop()
        own: list[ast.AST] = []
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, _SCOPES):
                pending.append(node)
                continue
            own.append(node)
            stack += ast.iter_child_nodes(node)
        yield own


class _Scope:
    def __init__(self, nodes: Iterable[ast.AST]) -> None:
        self.choices: set[str] = set()  # names bound to one choice of a chunk
        self.deltas: set[str] = set()
        nodes = list(nodes)
        for _ in range(3):  # delta from a choice name from a for loop: three steps at most
            before = (len(self.choices), len(self.deltas))
            for node in nodes:
                self._bind(node)
            if (len(self.choices), len(self.deltas)) == before:
                break

    def _bind(self, node: ast.AST) -> None:
        pairs: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            pairs = [(node.target, node.value)]
        elif (
            isinstance(node, ast.For | ast.AsyncFor | ast.comprehension)
            and isinstance(node.target, ast.Name)
            and self.choice_list(node.iter)
        ):
            self.choices.add(node.target.id)
        for target, value in pairs:
            if not isinstance(target, ast.Name):
                continue
            if self.delta(value):
                self.deltas.add(target.id)
            elif self.choice(value):
                self.choices.add(target.id)

    @staticmethod
    def choice_list(node: ast.AST) -> bool:
        return _key(node) == "choices"

    def choice(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.choices
        return (
            isinstance(node, ast.Subscript) and _key(node) is None and self.choice_list(node.value)
        )

    def delta(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.deltas
        return (
            _key(node) == "delta"
            and isinstance(node, ast.Attribute | ast.Subscript)
            and self.choice(node.value)
        )


# --- deltas known to be dicts ---------------------------------------------------------------


def _dict_type(node: ast.AST) -> bool:
    if isinstance(node, ast.Tuple):
        return bool(node.elts) and all(_dict_type(e) for e in node.elts)
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    return name in _DICT_TYPES


def _guarded(test: ast.AST) -> set[str]:
    """Names a true `test` shows to be dicts: `isinstance(x, dict)`, also inside `and`."""
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return set().union(*(_guarded(v) for v in test.values))
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "isinstance"
        and len(test.args) == 2
        and isinstance(test.args[0], ast.Name)
        and _dict_type(test.args[1])
    ):
        return {test.args[0].id}
    return set()


def _negated(test: ast.AST) -> set[str]:
    """Names a false `test` shows to be dicts: `not isinstance(x, dict)`."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _guarded(test.operand)
    return set()


def _guards(node: ast.AST, parents: dict[int, ast.AST]) -> list[tuple[set[str], list[ast.AST]]]:
    """(dict-guarded names, the guarded code) for each guard around `node` in its scope: the
    body of `if isinstance(x, dict):` (the else of `if not isinstance(...)`), statements after
    `if not isinstance(x, dict): continue` (or break / return / raise), later operands of
    `isinstance(x, dict) and ...`, the first branch of a conditional expression."""
    out: list[tuple[set[str], list[ast.AST]]] = []
    child = node
    while (parent := parents.get(id(child))) is not None and not isinstance(parent, _SCOPES):
        if isinstance(parent, ast.If | ast.IfExp):
            body = parent.body if isinstance(parent.body, list) else [parent.body]
            orelse = parent.orelse if isinstance(parent.orelse, list) else [parent.orelse]
            if any(child is b for b in body):
                out.append((_guarded(parent.test), body))
            elif any(child is b for b in orelse):
                out.append((_negated(parent.test), orelse))
        elif isinstance(parent, ast.BoolOp) and isinstance(parent.op, ast.And):
            i = next(i for i, v in enumerate(parent.values) if v is child)
            out.append((set().union(*(_guarded(v) for v in parent.values[:i])), [parent]))
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(parent, field, None)
            if not isinstance(stmts, list) or not any(child is s for s in stmts):
                continue
            i = next(i for i, s in enumerate(stmts) if s is child)
            for j, s in enumerate(stmts[:i]):
                if isinstance(s, ast.If) and not s.orelse and isinstance(s.body[-1], _EXITS):
                    out.append((_negated(s.test), stmts[j + 1 :]))
        child = parent
    return [(names, code) for names, code in out if names]


def dict_guarded(node: ast.AST, name: str, parents: dict[int, ast.AST]) -> bool:
    """Whether `node` sits in code a dict guard on `name` covers (_guards)."""
    return any(name in names for names, _ in _guards(node, parents))


def _assignments(nodes: Iterable[ast.AST]) -> dict[str, list[ast.AST]]:
    """name -> the values assigned to it (for a loop target: what it iterates)."""
    out: dict[str, list[ast.AST]] = {}
    for node in nodes:
        pairs: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            pairs = [(node.target, node.value)]
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            pairs = [(node.target, node.iter)]
        for target, value in pairs:
            if isinstance(target, ast.Name):
                out.setdefault(target.id, []).append(value)
    return out


def _flag(call: ast.Call, name: str, default: bool) -> bool:
    for kw in call.keywords:
        if kw.arg == name:
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return default


def _dict_source(node: ast.AST) -> bool:
    """A call whose result holds `content` only when it has a value (as 0.x's dicts did):
    json.loads / json.load (of anything but a dump that keeps None), to_dict() / to_json()
    unless exclude_unset=False, model_dump() / model_dump_json() with exclude_unset=True or
    exclude_none=True."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    func = node.func
    if isinstance(func.value, ast.Name) and func.value.id == "json":
        if func.attr not in ("loads", "load") or len(node.args) != 1:
            return False
        arg = node.args[0]
        dump = isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
        if dump and arg.func.attr in ("model_dump_json", "to_json"):  # type: ignore[union-attr]
            return _dict_source(arg)
        return True
    none = _flag(node, "exclude_none", False)
    if func.attr in ("to_dict", "to_json"):
        return none or _flag(node, "exclude_unset", True)
    if func.attr in ("model_dump", "model_dump_json"):
        return none or _flag(node, "exclude_unset", False)
    return False


def _derives(
    node: ast.AST,
    is_root: Callable[[ast.AST], bool],
    near: dict[str, list[ast.AST]],
    scope: dict[str, list[ast.AST]],
    depth: int = _DEPTH,
) -> bool:
    """`node`, stripped of subscripts and attributes, is a root, or a name every value of
    which derives from one (values assigned in the guarded code if any, else in the scope)."""
    while isinstance(node, ast.Subscript | ast.Attribute):
        node = node.value
    if is_root(node):
        return True
    if not isinstance(node, ast.Name) or depth == 0:
        return False
    values = near.get(node.id) or scope.get(node.id) or []
    return bool(values) and all(_derives(v, is_root, near, scope, depth - 1) for v in values)


def _known_dict(
    check: ast.AST, delta: ast.AST, parents: dict[int, ast.AST], scope: dict[str, list[ast.AST]]
) -> bool:
    if _derives(delta, _dict_source, {}, scope):
        return True
    for names, code in _guards(check, parents):
        near = _assignments(n for c in code for n in _own_nodes(c))
        guard = lambda n, names=names: isinstance(n, ast.Name) and n.id in names  # noqa: E731
        if _derives(delta, guard, near, scope):
            return True
    return False


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """`node` and its descendants, without nested scopes."""
    stack = [node]
    while stack:
        cur = stack.pop()
        yield cur
        stack += [c for c in ast.iter_child_nodes(cur) if not isinstance(c, _SCOPES)]


def _is_content(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "content"


def content_checks(tree: ast.Module, text: str) -> list[ContentCheck]:
    """Every `hasattr(<delta>, "content")` and `"content" [not] in <delta>`, in source order,
    except `in` checks on a delta known to be a dict (module docstring)."""
    parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    out = []
    for nodes in _scopes(tree):
        scope = _Scope(nodes)
        assigned: dict[str, list[ast.AST]] | None = None
        for node in nodes:
            delta: ast.AST | None = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "hasattr"
                and len(node.args) == 2
                and _is_content(node.args[1])
            ):
                delta = node.args[0]
            elif (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In | ast.NotIn)
                and _is_content(node.left)
            ):
                delta = node.comparators[0]
                if scope.delta(delta):
                    assigned = assigned if assigned is not None else _assignments(nodes)
                    if _known_dict(node, delta, parents, assigned):
                        continue
            if delta is not None and scope.delta(delta):
                out.append(ContentCheck(node, _source(node, text), _source(delta, text)))  # type: ignore[arg-type]
    return sorted(out, key=lambda c: (c.node.lineno, c.node.col_offset))
