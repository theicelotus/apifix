"""Streaming checks shared by the scanner and the `streaming` rewrite check.

Three things change: `stream=True` becomes a separate method
(`generate_content_stream`/`send_message_stream`), the async stream must be awaited before
`async for`, and a streamed chunk's `.text` can be None (no text, e.g. a function-call-only
chunk), so concatenating it unguarded (`+= chunk.text`) raises on the first such chunk.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

_MAX_SOURCE = 80


@dataclass(frozen=True)
class StreamError:
    node: ast.AST
    message: str

    @property
    def line(self) -> int:
        return self.node.lineno


def _source(node: ast.AST, text: str) -> str:
    segment = " ".join((ast.get_source_segment(text, node) or ast.unparse(node)).split())
    return segment if len(segment) <= _MAX_SOURCE else segment[: _MAX_SOURCE - 3] + "..."


def _calls_stream(node: ast.expr) -> bool:
    call = node.value if isinstance(node, ast.Await) else node
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in ("generate_content_stream", "send_message_stream")
    )


def _is_await(node: ast.expr) -> bool:
    return isinstance(node, ast.Await)


def _unawaited_async_for(tree: ast.Module, text: str) -> list[StreamError]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFor) and _calls_stream(node.iter) and not _is_await(node.iter):
            out.append(
                StreamError(
                    node.iter,
                    f"`{_source(node.iter, text)}` must be awaited before `async for` "
                    "(the async stream method returns a coroutine of an async iterator): "
                    f"`async for chunk in await {_source(node.iter, text)}`",
                )
            )
    return out


def _unguarded_chunk_text(tree: ast.Module, text: str) -> list[StreamError]:
    """`total += chunk.text` (or `total = total + chunk.text`) inside a loop over a stream call,
    where `chunk` is the loop's own target: a chunk may have no text."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For | ast.AsyncFor):
            continue
        source = node.iter.value if isinstance(node.iter, ast.Await) else node.iter
        if not (_calls_stream(source) and isinstance(node.target, ast.Name)):
            continue
        target = node.target.id
        for stmt in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            value = None
            if isinstance(stmt, ast.AugAssign) and isinstance(stmt.op, ast.Add):
                value = stmt.value
            elif (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.BinOp)
                and isinstance(stmt.value.op, ast.Add)
            ):
                value = stmt.value.right
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "text"
                and isinstance(value.value, ast.Name)
                and value.value.id == target
            ):
                out.append(
                    StreamError(
                        stmt,
                        f"`{_source(stmt, text)}`: a streamed chunk's `.text` may be None "
                        f'(e.g. a function-call-only chunk); use `{target}.text or ""`',
                    )
                )
    return out


def _leftover_stream_kwarg(tree: ast.Module, text: str) -> list[StreamError]:
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("generate_content", "send_message")
        ):
            for kw in node.keywords:
                if kw.arg == "stream":
                    out.append(
                        StreamError(
                            kw,
                            f"`{node.func.attr}` takes no `stream=` in the new SDK: use "
                            f"`{node.func.attr}_stream(...)` instead",
                        )
                    )
    return out


def stream_errors(tree: ast.Module, text: str) -> list[StreamError]:
    """Every streaming problem in `tree`, in source order."""
    errors = (
        _unawaited_async_for(tree, text)
        + _unguarded_chunk_text(tree, text)
        + _leftover_stream_kwarg(tree, text)
    )
    return sorted(errors, key=lambda e: (e.node.lineno, e.node.col_offset))


def streaming_check_errors(path: str, original: str, candidate: str) -> list[str]:
    """The `streaming` rewrite check: every streaming problem in the rewritten file
    (Migration.rewrite_checker). Lines are the candidate's."""
    if not path.endswith(".py"):
        return []
    try:
        new = ast.parse(candidate)
    except SyntaxError:
        return []
    return [f"line {e.line}: {e.message}" for e in stream_errors(new, candidate)]
