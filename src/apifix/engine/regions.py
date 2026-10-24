"""Region edits: for a large file, the model gets only the regions that need changing (each
top-level statement, or method, holding a finding) plus the file's import block, and returns
each region in its own labelled code block; apifix splices them back by position.

Regions are computed on the original file. Every version apifix checks is the original with
each region replaced by its current text (`splice`), so the whole-file checks run on a
complete file. Also here: the fenced-block and placeholder primitives whole-file replies share.

Pure functions on source text (ast + stdlib).
"""

from __future__ import annotations

import ast
import builtins
import difflib
import io
import re
import tokenize
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from apifix.engine.code_checks import top_level_names

IMPORTS = "IMPORTS"
NAMES_CHARS = 2_000  # read-only "defined elsewhere in this file" section
OUTLINE_CHARS = 1_000
MIN_LINES_FOR_LENGTH_CHECK = 20

Kind = Literal[
    "imports", "function", "class", "class_header", "method", "class_statement", "statement"
]
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_BUILTINS = frozenset(dir(builtins))


# --- fenced blocks and placeholders (shared with whole-file replies) --------------------------

_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*$")


@dataclass(frozen=True)
class Block:
    info: str  # the opening fence's info string, stripped
    body: str
    before: str  # the last non-blank line before the block (after the previous block), or ""


def fenced_blocks(reply: str) -> tuple[list[Block], bool]:
    """Every complete fenced code block (CommonMark-style fences: 3+ backticks or tildes, the
    closer at least as long as the opener), and whether a block was left unclosed."""
    lines = reply.splitlines()
    blocks: list[Block] = []
    i = 0
    last = ""
    while i < len(lines):
        m = _OPEN.match(lines[i])
        if not m or (m.group(1)[0] == "`" and "`" in m.group(2)):
            if lines[i].strip():
                last = lines[i].strip()
            i += 1
            continue
        fence = m.group(1)
        for j in range(i + 1, len(lines)):
            c = _CLOSE.match(lines[j])
            if c and c.group(1)[0] == fence[0] and len(c.group(1)) >= len(fence):
                blocks.append(Block(m.group(2).strip(), "\n".join(lines[i + 1 : j]), last))
                last = ""
                i = j + 1
                break
        else:
            return blocks, True
    return blocks, False


_ELISION_COMMENT = re.compile(
    r"^#\s*[(\[]?\s*(\.\.\.|…)"
    r"|^#.*\b(rest of (the )?\w+|remaining (code|lines|functions|methods|tests|parts?)"
    r"|everything else|omitted|for brevity|same as (before|above|the original|original))\b",
    re.IGNORECASE,
)


def placeholder_problem(candidate: str, original: str) -> str | None:
    """The first line of `candidate` that stands in for code ("# ... rest unchanged", a bare
    "...") and is not already in `original`."""
    known = {line.strip() for line in original.splitlines()}
    for n, line in enumerate(candidate.splitlines(), 1):
        s = line.strip()
        if s in known:
            continue
        if s in ("...", "…") or _ELISION_COMMENT.search(s):
            return f"line {n} is a placeholder instead of code ({s!r})"
    return None


def too_short(candidate: str, original: str) -> str | None:
    """Fewer than half the original's lines, when it has at least MIN_LINES_FOR_LENGTH_CHECK."""
    old, new = len(original.splitlines()), len(candidate.splitlines())
    if old >= MIN_LINES_FOR_LENGTH_CHECK and new * 2 < old:
        return f"it has {new} lines, fewer than half of the original's {old}"
    return None


# --- regions ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Region:
    id: str  # "R1", "R2", ... in source order, or IMPORTS
    kind: Kind
    name: str  # "ask", "Bot.ask", "Bot" (class kinds), "" (statements, imports)
    start: int  # first line, 1-based (decorators included)
    end: int  # last line, inclusive; start - 1 for an empty import block
    indent: str = ""  # leading whitespace of the region's first statement
    body_indent: str = ""  # class_header: indentation of the class body
    source: Literal["finding", "feedback", "imports"] = "finding"

    @property
    def lines(self) -> int:
        return self.end - self.start + 1

    def describe(self) -> str:
        if self.kind == "imports":
            return "the import block"
        what = {
            "function": f"def {self.name}",
            "class": f"class {self.name}",
            "class_header": f"header of class {self.name}",
            "method": f"method {self.name}",
            "class_statement": f"statement in class {self.name}",
            "statement": "module-level statement",
        }[self.kind]
        return f"{what} (original lines {self.start}-{self.end})"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "lines": [self.start, self.end],
            "source": self.source,
        }


@dataclass(frozen=True)
class RegionPlan:
    imports: Region
    regions: tuple[Region, ...]  # source order

    def get(self, region_id: str) -> Region:
        if region_id == IMPORTS:
            return self.imports
        return next(r for r in self.regions if r.id == region_id)

    @property
    def ids(self) -> list[str]:
        return [r.id for r in self.regions]

    def all(self) -> list[Region]:
        """The import block and every region, in file order."""
        return sorted((self.imports, *self.regions), key=lambda r: (r.start, r.end))

    def holding(self, line: int) -> Region | None:
        """The region (or import block) whose original lines include `line`."""
        return next((r for r in self.all() if r.start <= line <= r.end), None)

    def nearest(self, line: int) -> Region:
        """The region holding original `line`, else the nearest one above it, else below."""
        held = self.holding(line)
        if held:
            return held
        above = [r for r in self.all() if r.end < line and r.lines > 0]
        if above:
            return above[-1]
        below = [r for r in self.all() if r.lines > 0]
        return below[0] if below else self.imports


def split_lines(text: str) -> list[str]:
    """Lines with their endings, split at "\\n" only (as ast counts lines; str.splitlines
    also splits at form feeds and Unicode separators)."""
    parts = text.split("\n")
    lines = [p + "\n" for p in parts[:-1]]
    return lines + [parts[-1]] if parts[-1] else lines


def region_text(original: str, region: Region) -> str:
    return "".join(split_lines(original)[region.start - 1 : region.end])


def _start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([node.lineno, *(d.lineno for d in decorators)])  # type: ignore[attr-defined]


def _end(node: ast.AST) -> int:
    return node.end_lineno or node.lineno  # type: ignore[attr-defined]


def _indent(lines: list[str], line: int) -> str:
    text = lines[line - 1]
    return text[: len(text) - len(text.lstrip(" \t"))]


def _imports_only(stmts: Iterable[ast.stmt]) -> bool:
    return all(isinstance(s, ast.Import | ast.ImportFrom) for s in stmts)


def _type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_like(node: ast.stmt) -> bool:
    """A statement of the leading import block: an import, a try whose body holds only
    imports (handlers may assign fallbacks), or an `if TYPE_CHECKING:` holding only imports."""
    if isinstance(node, ast.Import | ast.ImportFrom):
        return True
    if isinstance(node, ast.Try):
        fallback = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.Pass)
        handlers = [s for h in node.handlers for s in h.body] + node.orelse + node.finalbody
        return _imports_only(node.body) and all(isinstance(s, fallback) for s in handlers)
    if isinstance(node, ast.If):
        return _type_checking(node.test) and _imports_only(node.body + node.orelse)
    return False


def _docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _import_block(tree: ast.Module) -> Region:
    """The leading run of import statements after the docstring and `__future__` imports; an
    empty block (end = start - 1) after them when the file has none."""
    body = tree.body
    i, after = 0, 0
    if body and _docstring(body[0]):
        i, after = 1, _end(body[0])
    while i < len(body) and isinstance(body[i], ast.ImportFrom) and body[i].module == "__future__":
        after = _end(body[i])
        i += 1
    first = last = None
    for node in body[i:]:
        if not _import_like(node):
            break
        first = first or _start(node)
        last = _end(node)
    if first is None or last is None:
        return Region(IMPORTS, "imports", "", after + 1, after, source="imports")
    return Region(IMPORTS, "imports", "", first, last, source="imports")


def _holding(nodes: Iterable[ast.stmt], line: int) -> ast.stmt | None:
    return next((n for n in nodes if _start(n) <= line <= _end(n)), None)


def _span_of(tree: ast.Module, lines: list[str], line: int) -> tuple | None:
    """(start, end, kind, name, indent, body_indent) of the region holding `line`."""
    node = _holding(tree.body, line)
    if node is None:
        return None
    start, end = _start(node), _end(node)
    if isinstance(node, _DEFS):
        return start, end, "function", node.name, "", ""
    if not isinstance(node, ast.ClassDef):
        return start, end, "statement", "", "", ""
    first = node.body[0]
    if _start(first) <= node.lineno:  # `class A: pass` on one line
        return start, end, "class", node.name, "", ""
    if line < _start(first):
        indent = _indent(lines, first.lineno)
        return start, _start(first) - 1, "class_header", node.name, "", indent
    member = _holding(node.body, line)
    if member is None:  # a comment or blank line between members
        return None
    m_start, m_end = _start(member), _end(member)
    indent = _indent(lines, m_start)
    if isinstance(member, _DEFS):
        return m_start, m_end, "method", f"{node.name}.{member.name}", indent, ""
    return m_start, m_end, "class_statement", node.name, indent, ""


def find_regions(
    original: str,
    finding_lines: Iterable[int],
    feedback_lines: Iterable[int] = (),
    previous: RegionPlan | None = None,
) -> RegionPlan | None:
    """The regions holding `finding_lines` (and `feedback_lines`: lines the verifier's output
    points at), and the import block. None when the file does not parse. With `previous`, a
    region with the same span keeps its id and new ones continue the numbering."""
    try:
        tree = ast.parse(original)
    except (SyntaxError, ValueError):
        return None
    lines = split_lines(original)
    imports = _import_block(tree)
    spans: dict[tuple[int, int], tuple] = {}
    sources: dict[tuple[int, int], str] = {}
    for source, line_set in (("finding", finding_lines), ("feedback", feedback_lines)):
        for line in sorted(set(line_set)):
            if imports.start <= line <= imports.end:
                continue
            span = _span_of(tree, lines, line)
            if span is None:
                continue
            key = (span[0], span[1])
            spans.setdefault(key, span)
            sources.setdefault(key, source)
    known = {(r.start, r.end): r.id for r in previous.regions} if previous else {}
    taken = [int(i[1:]) for i in known.values()]
    next_number = max(taken, default=0) + 1
    regions = []
    for key in sorted(spans):
        start, end, kind, name, indent, body_indent = spans[key]
        if key in known:
            region_id = known[key]
        elif previous is None:
            region_id = f"R{len(regions) + 1}"
        else:
            region_id, next_number = f"R{next_number}", next_number + 1
        regions.append(Region(region_id, kind, name, start, end, indent, body_indent, sources[key]))
    return RegionPlan(imports, tuple(regions))


def coverage(original: str, plan: RegionPlan) -> float:
    """The share of the file's characters in its regions and import block."""
    if not original:
        return 1.0
    return sum(len(region_text(original, r)) for r in plan.all()) / len(original)


# --- read-only context: names used, outline ---------------------------------------------------


def _nodes(tree: ast.Module, region: Region) -> list[ast.AST]:
    """The AST nodes making up a region."""
    out: list[ast.AST] = []
    for node in tree.body:
        if region.start <= _start(node) and _end(node) <= region.end:
            out.append(node)
        elif isinstance(node, ast.ClassDef) and _start(node) <= region.start <= _end(node):
            if region.kind == "class_header":
                out += [*node.decorator_list, *node.bases, *node.keywords]
            out += [m for m in node.body if region.start <= _start(m) and _end(m) <= region.end]
    return out


def _bound(nodes: Iterable[ast.AST]) -> set[str]:
    names: set[str] = set()
    for top in nodes:
        for n in ast.walk(top):
            if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load):
                names.add(n.id)
            elif isinstance(n, ast.arg):
                names.add(n.arg)
            elif isinstance(n, (*_DEFS, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.alias):
                names.add(n.asname or n.name.split(".")[0])
            elif isinstance(n, ast.ExceptHandler) and n.name:
                names.add(n.name)
    return names


def _loaded(nodes: Iterable[ast.AST]) -> set[str]:
    return {
        n.id
        for top in nodes
        for n in ast.walk(top)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _self_calls(nodes: Iterable[ast.AST]) -> set[str]:
    return {
        n.attr
        for top in nodes
        for n in ast.walk(top)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id in ("self", "cls")
    }


def _defines(node: ast.stmt) -> set[str]:
    if isinstance(node, (*_DEFS, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
    if isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, ast.Import | ast.ImportFrom):
        return {a.asname or a.name.split(".")[0] for a in node.names}
    return set()


@dataclass(frozen=True)
class _Entry:
    line: int
    full: str
    short: str
    name: str


def _signature(lines: list[str], node: ast.stmt) -> str:
    """A def/class's decorators and header line(s), without its body."""
    body = node.body  # type: ignore[attr-defined]
    last = max(node.lineno, _start(body[0]) - 1) if body else _end(node)
    return "".join(lines[_start(node) - 1 : last]).rstrip() + "  # body not shown"


def _entry(lines: list[str], node: ast.stmt, name: str) -> _Entry:
    start, end = _start(node), _end(node)
    head = f"# line {start}\n"
    if isinstance(node, (*_DEFS, ast.ClassDef)):
        sig = head + _signature(lines, node)
        return _Entry(start, sig, sig, name)
    first = lines[start - 1].rstrip()
    short = head + first + (f"  # {end - start} more lines not shown" if end > start else "")
    full = head + "".join(lines[start - 1 : end]).rstrip() if end - start < 3 else short
    return _Entry(start, full, short, name)


def names_context(original: str, plan: RegionPlan, ids: Sequence[str] | None = None) -> str:
    """Definitions outside the sent regions that the sent regions use: signatures of defs and
    classes, short assignments whole, longer ones by their first line; for methods, their
    class's header and class-level assignments, and the signatures of the other methods they
    call through self./cls. At most NAMES_CHARS ("" when there is nothing to show)."""
    tree = ast.parse(original)
    lines = split_lines(original)
    sent = [r for r in plan.regions if ids is None or r.id in ids]
    nodes = [n for r in sent for n in _nodes(tree, r)]
    import_nodes = [n for n in tree.body if plan.imports.start <= _start(n) <= plan.imports.end]
    wanted = _loaded(nodes) - _bound(nodes) - _bound(import_nodes) - _BUILTINS

    def inside_sent(node: ast.AST) -> bool:
        return any(r.start <= _start(node) and _end(node) <= r.end for r in sent)

    entries: list[_Entry] = []
    seen: set[int] = set()
    for node in tree.body:
        if node in import_nodes or inside_sent(node):
            continue
        hits = sorted(_defines(node) & wanted)
        if hits and id(node) not in seen:
            seen.add(id(node))
            entries.append(_entry(lines, node, hits[0]))
    classes = {r.name.split(".")[0] for r in sent if r.kind in ("method", "class_statement")}
    called = _self_calls(nodes)
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name in classes):
            continue
        first = node.body[0]
        header = "".join(lines[_start(node) - 1 : max(node.lineno, _start(first) - 1)])
        head_text = f"# line {_start(node)}\n{header.rstrip()}"
        entries.append(_Entry(_start(node), head_text, head_text, node.name))
        for member in node.body:
            if inside_sent(member):
                continue
            if isinstance(member, ast.Assign | ast.AnnAssign):
                entries.append(_entry(lines, member, f"{node.name}.<attribute>"))
            elif isinstance(member, _DEFS) and member.name in called:
                entries.append(_entry(lines, member, f"{node.name}.{member.name}"))
    if not entries:
        return ""
    entries.sort(key=lambda e: e.line)
    for form in ("full", "short"):
        text = "\n".join(getattr(e, form) for e in entries)
        if len(text) <= NAMES_CHARS:
            return text
    text = "\n".join(f"# line {e.line}: {e.name}" for e in entries)
    if len(text) > NAMES_CHARS:
        text = text[:NAMES_CHARS].rsplit("\n", 1)[0] + "\n# ... (more not shown)"
    return text


def outline(original: str, plan: RegionPlan) -> str:
    """One line per top-level def/class (and method), with its line range; regions marked.
    At most OUTLINE_CHARS."""
    tree = ast.parse(original)
    marks = {(r.start, r.end): r.id for r in plan.all() if r.lines > 0}

    def line(node: ast.stmt, indent: str, label: str) -> str:
        span = (_start(node), _end(node))
        mark = f"  [{marks[span]}]" if span in marks else ""
        return f"{indent}{span[0]}-{span[1]} {label}{mark}"

    out = []
    if plan.imports.lines > 0:
        out.append(f"{plan.imports.start}-{plan.imports.end} imports  [{IMPORTS}]")
    for node in tree.body:
        if isinstance(node, _DEFS):
            out.append(line(node, "", f"def {node.name}"))
        elif isinstance(node, ast.ClassDef):
            out.append(line(node, "", f"class {node.name}"))
            for member in node.body:
                if isinstance(member, _DEFS):
                    out.append(line(member, "  ", f"def {member.name}"))
                elif (_start(member), _end(member)) in marks:
                    out.append(line(member, "  ", "statement"))
        elif (_start(node), _end(node)) in marks:
            out.append(line(node, "", "statement"))
    text = ""
    for kept, entry in enumerate(out):
        if len(text) + len(entry) + 1 > OUTLINE_CHARS:
            return text + f"... ({len(out) - kept} more)"
        text += entry + "\n"
    return text.rstrip("\n")


# --- the request's region section -------------------------------------------------------------


def render_block(region: Region, text: str, fence: str, *, required: bool = False) -> str:
    """A region as the request shows it: a heading and a fenced block labelled with its id.
    The import block is optional unless `required` (a check retry that concerns it)."""
    if region.kind == "imports":
        where = (
            f"original lines {region.start}-{region.end}"
            if region.lines > 0
            else "the file has none yet"
        )
        optional = "" if required else "; return it only if you change it"
        head = f"Imports ({IMPORTS}, {where}{optional}):"
    else:
        head = f"Region {region.id}: {region.describe()}"
    body = text.rstrip("\n")
    return f"{head}\n{fence}python {region.id}\n{body}{chr(10) if body else ''}{fence}"


def region_instruction(ids: Sequence[str], fence: str = "```") -> str:
    """What to return: every id in `ids` (IMPORTS among them only when it is required)."""
    regions = [i for i in ids if i != IMPORTS]
    listed = ", ".join(ids)
    example = regions[0] if regions else IMPORTS
    text = (
        f"Return {'every region' if len(ids) > 1 else 'the region'} ({listed}), each complete "
        f"from its first line (decorators included) to its last, in its own fenced code block "
        f"with its label after the language ({fence}python {example})."
    )
    if IMPORTS not in ids:
        text += f" Return {IMPORTS} only if you change it;"
    else:
        text += f" In {IMPORTS}"
    return (
        text + " you may add imports, module-level assignments and module-level functions the "
        "migration needs there. Do not return the rest of the file."
    )


# --- parsing the reply ------------------------------------------------------------------------

_INFO_LABEL = re.compile(r"^(R\d+|IMPORTS)$", re.IGNORECASE)
_LINE_LABEL = re.compile(r"(?<![\w-])(R\d+|IMPORTS)(?![\w-])")


def _label(block: Block) -> str | None:
    for token in block.info.split():
        if _INFO_LABEL.match(token):
            return token.upper()
    found = set(_LINE_LABEL.findall(block.before))
    return found.pop() if len(found) == 1 else None


RegionOutcome = Literal["ok", "whole", "no_block", "truncated", "labels"]


@dataclass(frozen=True)
class RegionReply:
    outcome: RegionOutcome
    blocks: dict[str, str] = field(default_factory=dict)  # label -> block body
    labels: Literal["labelled", "by_order", ""] = ""
    problem: str = ""  # what to tell the model when outcome == "labels"
    body: str = ""  # outcome == "whole": the file


def labels_nudge(expected: Sequence[str], problem: str = "") -> str:
    return (
        f"Return each region in its own code block labelled with its id: {', '.join(expected)} "
        f"(and {IMPORTS} if you change it). {problem}"
    ).rstrip()


def _imports_shaped(body: str) -> bool:
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return False
    allowed = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, *_DEFS)
    return bool(tree.body) and all(isinstance(s, allowed) for s in tree.body)


def is_whole_file(body: str, original: str) -> bool:
    """A block that is the whole file rather than a region: it parses, defines every
    top-level name of the original, and has at least half its lines."""
    names, got = top_level_names(original), top_level_names(body)
    if names is None or got is None or not names <= got:
        return False
    return too_short(body, original) is None


def parse_region_blocks(
    reply: str, expected: Sequence[str], *, original: str = "", truncated: bool = False
) -> RegionReply:
    """The reply's region blocks by label (from the info string, else the line before the
    block). Unlabelled blocks are taken in order only when all are unlabelled and their number
    is the number of `expected` regions (or one more, the first being import-shaped: IMPORTS).
    One unlabelled block that is the whole `original` file is outcome "whole"."""
    if truncated:
        return RegionReply("truncated")
    blocks, unclosed = fenced_blocks(reply)
    if not blocks:
        return RegionReply("no_block")
    labels = [_label(b) for b in blocks]
    if all(label is None for label in labels):
        if len(blocks) == 1 and original and is_whole_file(blocks[0].body, original):
            return RegionReply("whole", body=blocks[0].body)
        bodies = [b.body for b in blocks]
        if len(bodies) == len(expected) + 1 and _imports_shaped(bodies[0]):
            return RegionReply(
                "ok", dict(zip([IMPORTS, *expected], bodies, strict=True)), "by_order"
            )
        if len(bodies) == len(expected) and not unclosed:
            return RegionReply("ok", dict(zip(expected, bodies, strict=True)), "by_order")
        return RegionReply("labels", problem="Your code blocks had no labels.")
    if None in labels:
        return RegionReply("labels", problem="Label every code block.")
    problems = []
    twice = sorted({label for label in labels if labels.count(label) > 1})
    unknown = [label for label in dict.fromkeys(labels) if label not in (*expected, IMPORTS)]
    missing = [label for label in expected if label not in labels]
    if missing:
        problems.append(f"Missing: {', '.join(missing)}.")
    if unknown:
        problems.append(f"Not requested: {', '.join(unknown)}.")
    if twice:
        problems.append(f"Returned twice: {', '.join(twice)}.")
    if unclosed:
        problems.append("A code block was not closed.")
    if problems:
        return RegionReply("labels", problem=" ".join(problems))
    return RegionReply(
        "ok", {label: b.body for label, b in zip(labels, blocks, strict=True)}, "labelled"
    )


# --- validating blocks and splicing -----------------------------------------------------------


def _string_inner_lines(text: str) -> set[int]:
    """1-based lines inside multi-line string tokens (after their first line): their leading
    whitespace is string content, not indentation."""
    inner: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.STRING and tok.end[0] > tok.start[0]:
                inner.update(range(tok.start[0] + 1, tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return inner


def _first_indent(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line[: len(line) - len(line.lstrip(" \t"))]
    return ""


def _shift(text: str, indent: str, *, add: bool) -> str:
    """Add `indent` to (or remove it from) every line that is not blank and not inside a
    multi-line string."""
    lines = text.split("\n")
    if add:
        inner = _string_inner_lines(text)
    else:
        inner = _string_inner_lines("\n".join(line.removeprefix(indent) for line in lines))
    out = []
    for n, line in enumerate(lines, 1):
        if n in inner or not line.strip():
            out.append(line)
        else:
            out.append(indent + line if add else line.removeprefix(indent))
    return "\n".join(out)


def _trim_blank(text: str) -> str:
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _statement_label(node: ast.stmt) -> str:
    if isinstance(node, _DEFS):
        return f"def {node.name}"
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    return type(node).__name__.lower()


_IMPORTS_ALLOWED = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.Pass, *_DEFS)


def _imports_problem(tree: ast.Module, original_block: str) -> str | None:
    try:
        known = {ast.dump(s) for s in ast.parse(original_block).body}
    except SyntaxError:
        known = set()

    def bad(stmts: Iterable[ast.stmt]) -> ast.stmt | None:
        for s in stmts:
            if ast.dump(s) in known or isinstance(s, _IMPORTS_ALLOWED):
                continue
            if isinstance(s, ast.Try):
                nested = s.body + [x for h in s.handlers for x in h.body] + s.orelse + s.finalbody
                found = bad(nested)
            elif isinstance(s, ast.If):
                found = bad(s.body + s.orelse)
            else:
                return s
            if found:
                return found
        return None

    found = bad(tree.body)
    if found is None:
        return None
    return (
        f"line {found.lineno} of your block: only imports, module-level assignments and "
        f"function definitions belong in {IMPORTS} (found {_statement_label(found)})"
    )


@dataclass(frozen=True)
class BlockResult:
    text: str = ""  # the block as it is spliced in (re-indented, outer blank lines removed)
    error: str = ""


def check_block(region: Region, original_text: str, block: str) -> BlockResult:
    """One returned block, before splicing: its indentation (a method returned at column 0 is
    re-indented), that it parses in its place and keeps the region's shape (a def stays one
    def of the same name; IMPORTS holds only imports, assignments and functions), and no
    placeholders or abbreviation against the region's original lines."""
    rid = region.id
    text = _trim_blank(block.replace("\r\n", "\n"))
    got = _first_indent(text)
    want = region.indent if region.kind != "class_header" else ""
    if got != want:
        if want and not got:
            text = _shift(text, want, add=True)
        elif not want:
            text = _shift(text, got, add=False)
        else:
            return BlockResult(error=f"{rid}: its indentation differs from the original's; keep it")
    if not text.strip() and region.kind not in ("statement", "class_statement", "imports"):
        return BlockResult(error=f"{rid}: the block is empty; return the whole region")

    if region.kind in ("method", "class_statement"):
        source, offset = "class _Region:\n" + (text or region.indent + "pass"), 1
    elif region.kind == "class_header":
        source, offset = text + "\n" + (region.body_indent or "    ") + "pass\n", 0
    else:
        source, offset = text, 0
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        where = f" (line {max((exc.lineno or 1) - offset, 1)} of your block)" if exc.lineno else ""
        return BlockResult(error=f"{rid}: SyntaxError: {exc.msg}{where}")
    in_class = region.kind in ("method", "class_statement")
    if in_class and len(tree.body) > 1:
        line = tree.body[1].lineno - offset
        return BlockResult(
            error=f"{rid}: line {line} of your block is outside the class; keep the indentation"
        )
    stmts = tree.body[0].body if in_class else tree.body  # type: ignore[attr-defined]

    shape = None
    if region.kind in ("function", "method"):
        name = region.name.rsplit(".", 1)[-1]
        if not (len(stmts) == 1 and isinstance(stmts[0], _DEFS) and stmts[0].name == name):
            found = ", ".join(_statement_label(s) for s in stmts) or "nothing"
            where = (
                f"; put new module-level functions in {IMPORTS}"
                if region.kind == "function"
                else ""
            )
            shape = f"return exactly def {name}, nothing before or after it (got: {found}){where}"
    elif region.kind in ("class", "class_header"):
        if not (
            len(stmts) == 1 and isinstance(stmts[0], ast.ClassDef) and stmts[0].name == region.name
        ):
            shape = f"return exactly the class {region.name}"
    elif region.kind == "imports":
        shape = _imports_problem(tree, original_text)
    if shape:
        return BlockResult(error=f"{rid}: {shape}")

    problem = placeholder_problem(text, original_text)
    if problem:
        return BlockResult(error=f"{rid}: {problem}; return the whole region")
    short = too_short(text, original_text) if region.kind != "imports" else None
    if short:
        return BlockResult(error=f"{rid}: {short}; return the whole region")
    return BlockResult(text=text)


@dataclass(frozen=True)
class Spliced:
    text: str
    # per line of `text` (index 0 = line 1): (region id, line within its text) for lines of a
    # region or the import block, (None, original line) for lines copied from the original
    origin: tuple[tuple[str | None, int], ...]

    def locate(self, line: int) -> tuple[str | None, int]:
        if 1 <= line <= len(self.origin):
            return self.origin[line - 1]
        return (None, line)


def splice(original: str, plan: RegionPlan, texts: Mapping[str, str]) -> Spliced:
    """The original with each region in `texts` (id -> text) replaced, from file order; other
    regions keep their original lines. Line endings and the final newline follow the
    original."""
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = split_lines(original)
    out: list[str] = []
    origin: list[tuple[str | None, int]] = []
    pos = 1
    for region in plan.all():
        for n in range(pos, region.start):
            out.append(lines[n - 1])
            origin.append((None, n))
        if region.id in texts:
            body = texts[region.id].replace("\r\n", "\n").removesuffix("\n")
            block = [line + newline for line in body.split("\n")] if body.strip() else []
        else:
            block = lines[region.start - 1 : region.end]
        for k, line in enumerate(block, 1):
            out.append(line if line.endswith("\n") else line + newline)
            origin.append((region.id, k))
        pos = max(pos, region.end + 1)
    for n in range(pos, len(lines) + 1):
        out.append(lines[n - 1])
        origin.append((None, n))
    text = "".join(out)
    if not original.endswith(("\n", "\r")) and text.endswith(newline):
        text = text[: -len(newline)]
    return Spliced(text, tuple(origin))


@dataclass(frozen=True)
class SpliceResult:
    text: str = ""  # the spliced file ("" when there are errors)
    texts: dict[str, str] = field(default_factory=dict)  # every region's text after this reply
    errors: list[str] = field(default_factory=list)
    spliced: Spliced | None = None


def apply_blocks(
    original: str,
    plan: RegionPlan,
    blocks: Mapping[str, str],
    base: Mapping[str, str] | None = None,
) -> SpliceResult:
    """Check each returned block (check_block) and splice them, over `base` (the regions'
    texts so far: kept regions of an earlier reply or batch). The spliced file must parse; a
    syntax error that only splicing reveals is reported on the region holding its line."""
    texts = dict(base or {})
    errors = []
    for label, block in blocks.items():
        region = plan.get(label)
        result = check_block(region, region_text(original, region), block)
        if result.error:
            errors.append(result.error)
        else:
            texts[label] = result.text
    if errors:
        return SpliceResult(texts=texts, errors=errors)
    spliced = splice(original, plan, texts)
    try:
        ast.parse(spliced.text)
    except SyntaxError as exc:
        rid, k = spliced.locate(exc.lineno or 0)
        where = f"{rid}, line {k} of your block" if rid else f"line {exc.lineno}"
        return SpliceResult(texts=texts, errors=[f"{where}: SyntaxError after splicing: {exc.msg}"])
    return SpliceResult(spliced.text, texts, [], spliced)


# --- after the checks: which regions an error concerns ----------------------------------------

_REGION_PREFIX = re.compile(r"^(R\d+|IMPORTS)(?:, line \d+ of your block)?: ")
_ORIGINAL_LINE = re.compile(r"^(after |before )?original line (\d+): ")
_CANDIDATE_LINE = re.compile(r"^line (\d+): ")
_TARGET = re.compile(r"mock target '([^']+)'|patch\(\"([^\"]+)\"\) does not affect")


def map_line(before: str, after: str, line: int) -> int:
    """The line of `before` that line `line` of `after` comes from (an inserted line: the
    line before the insertion, at least 1)."""
    a, b = split_lines(before), split_lines(after)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if j1 <= line - 1 < j2:
            if tag == "equal" or tag == "replace":
                return min(i1 + (line - 1 - j1), max(i2 - 1, i1)) + 1
            return max(i1, 1)
    return max(min(line, len(a)), 1)


def attribute_errors(
    errors: Sequence[str],
    plan: RegionPlan,
    spliced: Spliced | None,
    checked: str,
    texts: Mapping[str, str],
    fallback: Sequence[str],
) -> list[tuple[str, list[str]]]:
    """Each check error with the regions it concerns, rewritten where it names a line of the
    checked file ("line N: ..." becomes "R1, line K of your block: ..."). `spliced`: the
    splice the checked file (`checked`: it plus apifix's auto-imports and reverts) came from;
    `texts`: the regions' texts; `fallback`: the regions of errors that name no place."""
    out: list[tuple[str, list[str]]] = []
    for error in errors:
        m = _REGION_PREFIX.match(error)
        if m:
            out.append((error, [m.group(1)]))
            continue
        m = _ORIGINAL_LINE.match(error)
        if m:
            n = int(m.group(2))
            held = plan.holding(n) or (plan.holding(n + 1) if m.group(1) == "after " else None)
            out.append((error, [(held or plan.nearest(n)).id]))
            continue
        m = _CANDIDATE_LINE.match(error)
        if m and spliced is not None:
            rid, k = spliced.locate(map_line(spliced.text, checked, int(m.group(1))))
            if rid:
                out.append((f"{rid}, line {k} of your block: {error[m.end() :]}", [rid]))
            else:
                out.append((error, [plan.nearest(k).id]))
            continue
        m = _TARGET.search(error)
        if m:
            target = m.group(1) or m.group(2)
            hits = [
                r.id
                for r in plan.all()
                if f'"{target}"' in texts.get(r.id, "") or f"'{target}'" in texts.get(r.id, "")
            ]
            if hits:
                out.append((error, hits))
                continue
        out.append((error, list(fallback)))
    return out


def ordered_ids(plan: RegionPlan, ids: Iterable[str]) -> list[str]:
    """`ids` in file order, once each."""
    wanted = set(ids)
    return [r.id for r in plan.all() if r.id in wanted]


_HUNK = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@", re.MULTILINE)


def regions_diff(
    path: str,
    original: str,
    plan: RegionPlan,
    texts: Mapping[str, str],
    ids: Iterable[str],
    *,
    context: int = 1,
    limit: int | None = None,
) -> str:
    """A unified diff of each region in `ids` that `texts` changes, against its original
    lines (hunk headers in the original file's line numbers), at most `limit` characters."""
    parts = []
    for rid in ordered_ids(plan, ids):
        if rid not in texts:
            continue
        region = plan.get(rid)
        old = region_text(original, region)
        new = texts[rid].replace("\r\n", "\n")
        new = new + "\n" if new and not new.endswith("\n") else new
        if old.replace("\r\n", "\n") == new:
            continue
        diff = "".join(
            difflib.unified_diff(
                old.replace("\r\n", "\n").splitlines(keepends=True),
                new.splitlines(keepends=True),
                f"a/{path} {rid}",
                f"b/{path} {rid}",
                n=context,
            )
        )
        offset = max(region.start - 1, 0)

        def shift(m: re.Match[str], offset: int = offset) -> str:
            a, b = int(m.group(1)) + offset, int(m.group(3)) + offset
            return f"@@ -{a}{m.group(2) or ''} +{b}{m.group(4) or ''} @@"

        parts.append(_HUNK.sub(shift, diff))
    text = "".join(parts)
    if limit is not None and len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n... (diff truncated)"
    return text
