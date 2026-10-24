"""Key checks on a streamed chunk's delta: `hasattr(delta, "content")`, `"content" in delta`.

0.x stream deltas were dicts (OpenAIObject) that held `content` only when there was text, so
code checked for the key. In 1.x every delta has `content`, None on the role chunk and the final
one: `hasattr` is always true (the None is yielded) and `in` never is (a pydantic model is no
dict). A delta is recognised by its shape, 0.x or 1.x form: `<x>.choices[i].delta`,
`<x>["choices"][i]["delta"]` (also mixed), a name assigned from one, or `<c>.delta` /
`<c>["delta"]` where `c` was assigned from `<x>.choices[i]` or bound by `for c in <x>.choices`.
Names are per function scope and flow-insensitive. Used by the scanner (0.x code) and the
`streaming` rewrite check (whole-file mode).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

_MAX_SOURCE = 80
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


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


def _is_content(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "content"


def content_checks(tree: ast.Module, text: str) -> list[ContentCheck]:
    """Every `hasattr(<delta>, "content")` and `"content" [not] in <delta>`, in source order."""
    out = []
    for nodes in _scopes(tree):
        scope = _Scope(nodes)
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
            if delta is not None and scope.delta(delta):
                out.append(ContentCheck(node, _source(node, text), _source(delta, text)))  # type: ignore[arg-type]
    return sorted(out, key=lambda c: (c.node.lineno, c.node.col_offset))
