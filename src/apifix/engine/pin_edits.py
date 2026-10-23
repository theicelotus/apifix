"""Dependency-pin edits apifix makes itself, without an LLM.

For every dependency finding, the migrated library's pin gets the migration's `pin_spec`; in
the same file, pins of the library's own requirements that allow none of what it needs
(`Migration.companion_pins`) are raised. Only the version-spec characters change: names,
extras, markers, comments, quoting and line endings stay byte-for-byte.

Every file's edits are checked by parsing it again: the result may differ from the original
only in the intended entries, which must now carry the intended versions. An entry apifix
cannot edit this way (an unusual format, a failed check) is a fallback with the reason; the
caller sends such files to the model, except hash-pinned requirements files, which no edit
without the hashes can fix.

Stdlib only.
"""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from apifix.engine.dependencies import (
    CONDA_ENVIRONMENT_FILES,
    REQUIREMENT_OPTIONS,
    DeclaredDependency,
    conda_package_name,
    dependency_file_problem,
    entirely_below,
    normalize_name,
    parse_conda_environment,
    parse_dependency_file,
    parse_version,
    pep440_clauses,
    poetry_clauses,
)
from apifix.engine.migrations import Migration
from apifix.engine.models import CompanionPin, Finding

Clauses = tuple[tuple[str, str], ...]
EditKind = Literal["migration", "companion"]

HASH_PINNED = "hash-pinned; update manually"
LOCKFILES = {
    "pipfile": ("Pipfile.lock",),
    "pyproject.toml": ("poetry.lock", "pixi.lock", "uv.lock", "pdm.lock"),
    "pixi.toml": ("pixi.lock",),
}
_SETUP_PY_KEYS = ("install_requires", "extras_require", "tests_require")  # as the parser reads


@dataclass(frozen=True)
class PinEdit:
    file: str
    line: int
    old: str  # the entry's source text before the edit, e.g. `openai==0.27.6`
    new: str  # ... and after, e.g. `openai>=1.0,<2`
    kind: EditKind
    why: str = ""  # companion edits: what the migrated library needs


@dataclass(frozen=True)
class PinFallback:
    """An entry apifix did not edit, and why. `sent`: the file goes to the model instead
    (False: nothing in the job can fix it; the report asks for a manual update)."""

    file: str
    line: int
    entry: str
    reason: str
    sent: bool = True


@dataclass
class PinFile:
    """One dependency file with dependency findings: the edits apifix made in `text` (the
    original when there are none), the entries it could not edit, and report notes."""

    path: str
    original: str
    text: str
    edits: list[PinEdit] = field(default_factory=list)
    fallbacks: list[PinFallback] = field(default_factory=list)
    other_findings: bool = False  # the file has findings that are not dependency pins
    notes: list[str] = field(default_factory=list)

    @property
    def hash_pinned(self) -> bool:
        return any(not f.sent for f in self.fallbacks)

    @property
    def complete(self) -> bool:
        """Every finding of the file is handled: whole-file mode need not send it."""
        return bool(self.edits) and not self.fallbacks and not self.other_findings


@dataclass
class PinPlan:
    files: list[PinFile] = field(default_factory=list)

    def applied(self, whole_file: bool) -> tuple[list[PinFile], list[PinFallback]]:
        """The files whose edits are written, and every entry left to the model or to a manual
        update. Whole-file mode writes complete files only: a file it sends goes as the
        original, so the model sees one consistent file, and its edits become fallbacks too."""
        files, fallbacks = [], []
        for f in self.files:
            fallbacks += f.fallbacks
            if f.edits and (f.complete or not whole_file):
                files.append(f)
            elif f.edits and not f.hash_pinned:
                why = (
                    f.fallbacks[0].reason
                    if f.fallbacks
                    else "the file has other findings, so the model updates all of it"
                )
                fallbacks += [
                    PinFallback(f.path, e.line, e.old, f"sent whole: {why}")
                    for e in f.edits
                    if e.kind == "migration"
                ]
        return files, sorted(fallbacks, key=lambda b: (b.file, b.line))

    def write(self, work: Path, whole_file: bool) -> list[PinFile]:
        files, _ = self.applied(whole_file)
        for f in files:
            (work / f.path).write_bytes(f.text.encode("utf-8"))
        return files


def plan_pin_edits(root: Path, findings: list[Finding], migration: Migration) -> PinPlan:
    """What apifix can set itself in `root`'s dependency files with dependency findings."""
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)
    plan = PinPlan()
    for path in sorted(by_file):
        own = by_file[path]
        if not any(f.category == "dependency" for f in own):
            continue
        pin_file = _plan_file(root, path, own, migration)
        pin_file.other_findings = any(f.category != "dependency" for f in own)
        plan.files.append(pin_file)
    return plan


def pin_line(edit: PinEdit) -> str:
    """`environment.yml:158 openai==0.27.6 -> openai>=1.0,<2` (plus why, for companions)."""
    why = f" ({edit.why})" if edit.why else ""
    return f"{edit.file}:{edit.line} {edit.old} -> {edit.new}{why}"


def fallback_line(fb: PinFallback) -> str:
    where = "update manually" if not fb.sent else "sent to the model"
    reason = fb.reason if not fb.sent else f"{fb.reason}; {where}"
    return f"{fb.file}:{fb.line} ({reason})"


# --- one file ---------------------------------------------------------------------------------


@dataclass
class _Entry:
    """A located dependency entry: `start`/`end` delimit the characters that hold its version
    spec (the whole string for Poetry/Pipfile/pixi tables), `shown` the text the report quotes.
    `problem`: why it cannot be edited. `path`: its place in a TOML document."""

    name: str
    clauses: Clauses
    line: int
    start: int
    end: int
    shown: tuple[int, int]
    problem: str = ""
    path: tuple[Any, ...] = ()
    value: str = ""  # TOML: the string's value before the edit
    value_spec: tuple[int, int] = (0, 0)  # TOML: the spec's position within `value`


@dataclass
class _Target:
    entry: _Entry
    spec: str
    kind: EditKind
    why: str = ""


def _plan_file(root: Path, path: str, findings: list[Finding], migration: Migration) -> PinFile:
    raw = (root / path).read_bytes()
    dep_findings = [f for f in findings if f.category == "dependency"]

    def fail_all(reason: str, sent: bool = True) -> list[PinFallback]:
        return [PinFallback(path, f.line, f.old_pattern, reason, sent) for f in dep_findings]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return PinFile(path, "", "", fallbacks=fail_all("not UTF-8 text"))
    result = PinFile(path, text, text)
    try:
        entries = _entries(path, text)
        declared = parse_dependency_file(path, text)
    except (SyntaxError, ValueError, configparser.Error) as exc:
        result.fallbacks = fail_all(f"the file does not parse ({type(exc).__name__})")
        return result
    if _is_requirements(path) and re.search(r"(^|\s)--hash[=\s]", _strip_comments(text)):
        result.fallbacks = fail_all(HASH_PINNED, sent=False)
        return result

    flagged = [d for d in declared if migration.check_dependency(path, d)]
    targets: list[_Target] = []
    for e in entries:
        if migration.check_dependency(path, _declared(e)) is None:
            continue
        if e.problem:
            result.fallbacks.append(
                PinFallback(path, _line(e, declared), _label(text, e), e.problem)
            )
        else:
            targets.append(_Target(e, migration.pin_spec, "migration"))
    # entries the parser flags but no locator found (an unusual layout)
    located = Counter((e.name, e.clauses) for e in entries)
    for d in flagged:
        if located[(d.name, d.clauses)] > 0:
            located[(d.name, d.clauses)] -= 1
        else:
            result.fallbacks.append(
                PinFallback(path, d.line, d.raw, "could not locate the entry in the source")
            )
    if not targets:
        return result

    for e in entries:
        pin = migration.companion_pins.get(e.name)
        if pin is None or not _excludes(e.clauses, pin):
            continue
        spec = _companion_spec(e.clauses, pin)
        if e.problem:
            result.notes.append(
                f"not changed: {path}:{_line(e, declared)} {_label(text, e)} ({e.problem}); "
                f"{migration.package} needs {e.name}{spec}"
            )
            continue
        targets.append(_Target(e, spec, "companion", f"{migration.package} needs {e.name}{spec}"))

    new_text, problem = _apply(path, text, targets, migration)
    if problem and any(t.kind == "companion" for t in targets):
        # keep the library's own pin even when a raised companion pin is what failed
        companions = [t for t in targets if t.kind == "companion"]
        targets = [t for t in targets if t.kind == "migration"]
        new_text, problem = _apply(path, text, targets, migration)
        result.notes += [
            f"not changed: {path}:{t.entry.line} {_shown(text, t.entry)} (the re-parse check "
            f"failed); {t.why}"
            for t in companions
        ]
    if problem:
        result.fallbacks += [
            PinFallback(path, t.entry.line, _shown(text, t.entry), f"re-parse check: {problem}")
            for t in targets
        ]
        return result
    result.text = new_text
    result.edits = [
        PinEdit(path, t.entry.line, _shown(text, t.entry), _shown_after(text, t), t.kind, t.why)
        for t in sorted(targets, key=lambda t: t.entry.start)
    ]
    result.notes += _conda_notes(path, text, migration) + _lockfile_notes(root, path)
    return result


def _declared(e: _Entry) -> DeclaredDependency:
    return DeclaredDependency(e.name, "", e.clauses, e.line, 1)


def _shown(text: str, e: _Entry) -> str:
    return text[e.shown[0] : e.shown[1]]


def _label(text: str, e: _Entry) -> str:
    """The entry for a fallback line: its source text, or its value when it was not located."""
    return _shown(text, e) or e.value


def _line(e: _Entry, declared: list[DeclaredDependency]) -> int:
    """The entry's line; the parser's for an entry no locator placed."""
    if e.line:
        return e.line
    return next((d.line for d in declared if (d.name, d.clauses) == (e.name, e.clauses)), 1)


def _shown_after(text: str, t: _Target) -> str:
    e = t.entry
    return text[e.shown[0] : e.start] + t.spec + text[e.end : e.shown[1]]


def _splice(text: str, targets: list[_Target]) -> str:
    for t in sorted(targets, key=lambda t: t.entry.start, reverse=True):
        text = text[: t.entry.start] + t.spec + text[t.entry.end :]
    return text


# --- which pins, and to what --------------------------------------------------------------------


def _excludes(clauses: Clauses, pin: CompanionPin) -> bool:
    """True if the clauses allow no version in [pin.minimum, pin.upper)."""
    if not clauses:
        return False
    if entirely_below(clauses, pin.minimum):
        return True
    upper = parse_version(pin.upper)
    for op, version in clauses:
        v = parse_version(version)
        if (
            v is not None
            and upper is not None
            and op in (">=", ">", "==", "===", "~=")
            and v >= upper
        ):
            return True
    return False


def _companion_spec(clauses: Clauses, pin: CompanionPin) -> str:
    if not pin.keep_major:
        return f">={pin.minimum},<{pin.upper}"
    low, high = parse_version(pin.minimum), parse_version(pin.upper)
    assert low is not None and high is not None
    bounding = [v for op, v in clauses if op in ("==", "===", "~=", "<=", "<")] or [
        v for _, v in clauses
    ]
    current = parse_version(bounding[0]) if bounding else None
    major = min(max(current[0] if current else low[0], low[0]), high[0] - 1)
    floor = pin.minimum if major == low[0] else str(major)
    return f">={floor},<{major + 1}"


# --- locating entries ---------------------------------------------------------------------------

_REQ = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>[^;@#]*?)\s*(?:;.*)?$",
    re.DOTALL,
)
_COMMENT = re.compile(r"(^|\s)#")


@dataclass(frozen=True)
class _Req:
    name: str
    clauses: Clauses
    spec: tuple[int, int]  # offsets within the requirement string
    end: int  # where the name, extras and spec end (the shown part)


def _requirement(req: str) -> _Req | None:
    """A PEP 508 requirement's name, clauses and the position of its version spec, or None
    (direct references, options, unparseable text, no spec)."""
    head = req.split(";", 1)[0]
    if "@" in head or not req.strip() or req.lstrip().startswith(("-", "git+", "http:", "https:")):
        return None
    m = _REQ.match(req)
    if not m or not m.group("spec"):
        return None
    clauses = pep440_clauses(m.group("spec"))
    if not clauses:
        return None
    return _Req(normalize_name(m.group("name")), clauses, m.span("spec"), m.end("spec"))


def _req_entry(text: str, base: int, req: str, line: int, problem: str = "") -> _Entry | None:
    r = _requirement(req)
    if r is None:
        return None
    lead = len(req) - len(req.lstrip())
    return _Entry(
        r.name,
        r.clauses,
        line,
        base + r.spec[0],
        base + r.spec[1],
        (base + lead, base + r.end),
        problem,
    )


def _is_requirements(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name not in {
        "pyproject.toml",
        "pixi.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        *CONDA_ENVIRONMENT_FILES,
    }


def _strip_comments(text: str) -> str:
    return "\n".join(_cut_comment(line) for line in text.splitlines())


def _cut_comment(line: str) -> str:
    m = _COMMENT.search(line)
    return line[: m.start()] if m else line


def _lines(text: str) -> Iterator[tuple[int, int, str]]:
    """(line number, offset, line without its line ending)."""
    offset = 0
    for n, line in enumerate(text.splitlines(keepends=True), 1):
        yield n, offset, line.rstrip("\r\n")
        offset += len(line)


def _entries(path: str, text: str) -> list[_Entry]:
    name = PurePosixPath(path).name.lower()
    if name in ("pyproject.toml", "pixi.toml", "pipfile"):
        return _toml_entries(name, text)
    if name in CONDA_ENVIRONMENT_FILES:
        return _conda_entries(text)
    if name == "setup.py":
        return _setup_py_entries(text)
    if name == "setup.cfg":
        return _setup_cfg_entries(text)
    return _requirements_entries(text)


def _requirements_entries(text: str) -> list[_Entry]:
    out = []
    continued = False
    for n, offset, line in _lines(text):
        content = _cut_comment(line)
        problem = ""
        if continued or content.rstrip().endswith("\\"):
            problem = "the requirement continues over several lines"
        continued = content.rstrip().endswith("\\")
        req = content.rstrip().removesuffix("\\").rstrip()
        if not req.lstrip().startswith("-"):
            req = REQUIREMENT_OPTIONS.sub("", req)
        entry = _req_entry(text, offset, req, n, problem)
        if entry:
            out.append(entry)
    return out


def _conda_entries(text: str) -> list[_Entry]:
    env = parse_conda_environment(text)
    starts = {n: offset for n, offset, _ in _lines(text)}
    lines = text.splitlines()
    out = []
    for _, n, col in env.pip:
        item = lines[n - 1][col - 1 :]
        base = starts[n] + col - 1
        problem = ""
        if item[:1] in ("'", '"'):
            quote = item[0]
            close = item.find(quote, 1)
            inner = item[1:close] if close > 0 else ""
            if "\\" in inner or (quote == "'" and item[close + 1 : close + 2] == "'"):
                problem = "a quoted YAML string with escapes"
            content, base = inner, base + 1
        else:
            m = re.search(r"\s#", item)
            content = item[: m.start()] if m else item
        entry = _req_entry(text, base, content, n, problem)
        if entry:
            out.append(entry)
    return out


def _setup_cfg_entries(text: str) -> list[_Entry]:
    out = []
    section = option = ""
    for n, offset, line in _lines(text):
        header = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if header:
            section, option = header.group(1).strip(), ""
            continue
        if not line.strip() or line.strip()[0] in "#;":
            continue
        value_start = 0
        if not line[:1].isspace():
            m = re.match(r"^([^=:\s][^=:]*?)\s*[=:]\s*", line)
            if not m:
                option = ""
                continue
            option, value_start = m.group(1).strip().lower(), m.end()
        wanted = (section == "options" and option == "install_requires") or (
            section == "options.extras_require" and option
        )
        if not wanted:
            continue
        content = _cut_comment(line[value_start:])
        entry = _req_entry(text, offset + value_start, content, n)
        if entry:
            out.append(entry)
    return out


def _setup_py_entries(text: str) -> list[_Entry]:
    tree = ast.parse(text)
    starts = {n: offset for n, offset, _ in _lines(text)}
    lines = text.splitlines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg not in _SETUP_PY_KEYS:
                continue
            for const in _setup_strings(kw.value):
                r = _requirement(const.value)
                if r is None:
                    continue
                n = const.lineno
                line = lines[n - 1].encode("utf-8")
                col = len(line[: const.col_offset].decode("utf-8"))
                segment = line[const.col_offset : const.end_col_offset].decode("utf-8")
                plain = (
                    const.end_lineno == n
                    and len(segment) >= 2
                    and segment[0] in "'\""
                    and segment[-1] == segment[0]
                    and not segment.startswith(segment[0] * 3)
                    and segment[1:-1] == const.value
                )
                base = starts[n] + col + 1
                entry = _req_entry(
                    text,
                    base,
                    const.value,
                    n,
                    ""
                    if plain
                    else "not a plain string literal (prefix, escapes or concatenation)",
                )
                if entry and not plain:  # positions are only meaningful for plain literals
                    entry.start = entry.end = base
                    entry.shown = (starts[n] + col, starts[n] + col + len(segment))
                if entry:
                    out.append(entry)
    return out


def _setup_strings(node: ast.AST) -> Iterator[ast.Constant]:
    """String constants in a list/tuple/set/dict literal (dict values only), like the parser."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    elif isinstance(node, ast.List | ast.Tuple | ast.Set):
        for elt in node.elts:
            yield from _setup_strings(elt)
    elif isinstance(node, ast.Dict):
        for value in node.values:
            yield from _setup_strings(value)


# --- TOML ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Literal:
    start: int  # the opening quote
    end: int  # after the closing quote
    plain: bool  # single-line, no escapes: the text between the quotes is the value

    @property
    def inner(self) -> tuple[int, int]:
        return self.start + 1, self.end - 1


def _toml_literals(text: str) -> list[_Literal]:
    """String literals in a TOML document, in order (comments skipped). Only `plain` ones are
    edited; the rest are listed so that a value written that way is recognised."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            nl = text.find("\n", i)
            i = n if nl == -1 else nl
        elif text.startswith(('"""', "'''"), i):
            quote = text[i : i + 3]
            j = i + 3
            while j < n and not text.startswith(quote, j):
                j += 2 if quote == '"""' and text[j] == "\\" else 1
            end = min(n, j + 3)
            while end < n and text[end] == quote[0]:  # up to two quotes may end the value
                end += 1
            out.append(_Literal(i, end, False))
            i = end
        elif c in "\"'":
            j = i + 1
            escaped = False
            while j < n and text[j] != c and text[j] != "\n":
                if c == '"' and text[j] == "\\":
                    escaped = True
                    j += 1
                j += 1
            out.append(_Literal(i, j + 1, not escaped))
            i = j + 1
        else:
            i += 1
    return out


_TomlValue = tuple[tuple[Any, ...], str, str | None]


def _toml_values(name: str, doc: dict) -> Iterator[_TomlValue]:
    """(path, value, key) for every dependency declaration the parser reads: `key` is the
    package name of a `name = spec` table entry, None for a PEP 508 string."""

    def strings(path: tuple[Any, ...], items: object) -> Iterator[_TomlValue]:
        if isinstance(items, list):
            for i, item in enumerate(items):
                if isinstance(item, str):
                    yield (*path, i), item, None

    def table(path: tuple[Any, ...], tbl: object) -> Iterator[_TomlValue]:
        if not isinstance(tbl, dict):
            return
        for key, value in tbl.items():
            if isinstance(value, str):
                yield (*path, key), value, key
            elif isinstance(value, dict) and isinstance(value.get("version"), str):
                yield (*path, key, "version"), value["version"], key

    def at(path: tuple[Any, ...]) -> object:
        node: object = doc
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        return node

    if name == "pipfile":
        for section in ("packages", "dev-packages"):
            yield from table((section,), doc.get(section))
        return
    pixi: tuple[Any, ...] = () if name == "pixi.toml" else ("tool", "pixi")
    if name == "pyproject.toml":
        project = doc.get("project", {})
        yield from strings(("project", "dependencies"), project.get("dependencies"))
        for group, items in (project.get("optional-dependencies") or {}).items():
            yield from strings(("project", "optional-dependencies", group), items)
        for group, items in (doc.get("dependency-groups") or {}).items():
            yield from strings(("dependency-groups", group), items)
        poetry = ("tool", "poetry")
        yield from table((*poetry, "dependencies"), at((*poetry, "dependencies")))
        yield from table((*poetry, "dev-dependencies"), at((*poetry, "dev-dependencies")))
        for group, body in (at((*poetry, "group")) or {}).items():  # type: ignore[union-attr]
            if isinstance(body, dict):
                yield from table(
                    (*poetry, "group", group, "dependencies"), body.get("dependencies")
                )
    root = at(pixi) if pixi else doc
    if not isinstance(root, dict):
        return
    levels: list[tuple[tuple[Any, ...], dict]] = [(pixi, root)]
    for feature, body in (root.get("feature") or {}).items():
        if isinstance(body, dict):
            levels.append(((*pixi, "feature", feature), body))
    for base, level in levels:
        yield from table((*base, "pypi-dependencies"), level.get("pypi-dependencies"))
        for target, body in (level.get("target") or {}).items():
            if isinstance(body, dict):
                yield from table(
                    (*base, "target", target, "pypi-dependencies"), body.get("pypi-dependencies")
                )


def _changed_paths(a: object, b: object, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return [path]
        return [p for k in a for p in _changed_paths(a[k], b[k], (*path, k))]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [path]
        return [
            p
            for i, (x, y) in enumerate(zip(a, b, strict=True))
            for p in _changed_paths(x, y, (*path, i))
        ]
    return [] if type(a) is type(b) and a == b else [path]


def _toml_entries(name: str, text: str) -> list[_Entry]:
    doc = tomllib.loads(text)
    literals = _toml_literals(text)
    out = []
    for path, value, key in _toml_values(name, doc):
        pep508 = key is None
        if pep508:
            r = _requirement(value)
            if r is None:
                continue
            req_name, clauses, spec, shown_end = r.name, r.clauses, r.spec, r.end
        else:
            clauses = poetry_clauses(value)
            if not clauses:
                continue
            req_name, spec, shown_end = normalize_name(str(key)), (0, len(value)), len(value)
        probe = "apifix-probe-1f3c"
        new_value = value[: spec[0]] + probe + value[spec[1] :]
        found = None
        for lit in literals:
            lo, hi = lit.inner
            if not lit.plain or text[lo:hi] != value:
                continue
            trial = text[:lo] + new_value + text[hi:]
            try:
                changed = _changed_paths(doc, tomllib.loads(trial))
            except tomllib.TOMLDecodeError:
                continue
            if changed == [path]:
                found = lit
                break
        if found is None:
            problem = "could not locate the entry as a plain one-line TOML string"
            out.append(_Entry(req_name, clauses, 0, 0, 0, (0, 0), problem, path, value, spec))
            continue
        lo, _ = found.inner
        line = text.count("\n", 0, found.start) + 1
        line_start = text.rfind("\n", 0, found.start) + 1
        if pep508:
            shown = (lo, lo + shown_end)
        else:
            indent = len(text[line_start : found.start]) - len(
                text[line_start : found.start].lstrip()
            )
            shown = (line_start + indent, found.end)
        out.append(
            _Entry(
                req_name, clauses, line, lo + spec[0], lo + spec[1], shown, "", path, value, spec
            )
        )
    return out


# --- applying and checking ----------------------------------------------------------------------


def _apply(
    path: str, text: str, targets: list[_Target], migration: Migration
) -> tuple[str, str | None]:
    """The edited text, and why the re-parse check rejects it (None: it passes)."""
    new = _splice(text, targets)
    try:
        problem = _check(path, text, new, targets, migration)
    except (SyntaxError, ValueError, configparser.Error) as exc:
        problem = f"the edited file does not parse ({type(exc).__name__}: {exc})"
    return new, problem


def _check(
    path: str, old: str, new: str, targets: list[_Target], migration: Migration
) -> str | None:
    before, after = parse_dependency_file(path, old), parse_dependency_file(path, new)
    if [d.name for d in before] != [d.name for d in after]:
        return "the declared package names changed"
    changed = Counter(
        (b.name, b.clauses) for a, b in zip(before, after, strict=True) if a.clauses != b.clauses
    )
    expected = Counter((t.entry.name, pep440_clauses(t.spec)) for t in targets)
    if changed != expected:
        return "the edit did not change exactly the intended entries"
    flagged = [
        sum(migration.check_dependency(path, d) is not None for d in ds) for ds in (before, after)
    ]
    if flagged[1] != flagged[0] - sum(t.kind == "migration" for t in targets):
        return f"{migration.package} is still pinned below the migration's version"
    if dependency_file_problem(path, old) is None and (
        problem := dependency_file_problem(path, new)
    ):
        return problem
    name = PurePosixPath(path).name.lower()
    if name in ("pyproject.toml", "pixi.toml", "pipfile"):
        return _check_toml(old, new, targets)
    if name == "setup.py":
        return _check_setup_py(old, new, targets)
    if name == "setup.cfg":
        return _check_setup_cfg(old, new)
    if name in CONDA_ENVIRONMENT_FILES:
        a, b = parse_conda_environment(old), parse_conda_environment(new)
        if a.conda != b.conda or a.exported != b.exported or len(a.pip) != len(b.pip):
            return "the environment changed beyond the pip entries"
    return None


def _check_toml(old: str, new: str, targets: list[_Target]) -> str | None:
    a, b = tomllib.loads(old), tomllib.loads(new)
    expected = {}
    for t in targets:
        e = t.entry
        expected[e.path] = e.value[: e.value_spec[0]] + t.spec + e.value[e.value_spec[1] :]
    changed = _changed_paths(a, b)
    if set(changed) != set(expected):
        return "the document changed outside the intended values"
    for p, value in expected.items():
        node: Any = b
        for part in p:
            node = node[part]
        if node != value:
            return f"{'.'.join(map(str, p))} is {node!r}, expected {value!r}"
    return None


def _check_setup_py(old: str, new: str, targets: list[_Target]) -> str | None:
    """The AST is the same apart from the edited string constants."""
    a, b = ast.parse(old), ast.parse(new)
    wanted = {(t.entry.line, t.entry.start): t for t in targets}
    starts = {n: offset for n, offset, _ in _lines(old)}
    lines = old.splitlines()
    for node in ast.walk(a):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            col = len(lines[node.lineno - 1].encode("utf-8")[: node.col_offset].decode("utf-8"))
            base = starts[node.lineno] + col + 1
            for (line, start), t in wanted.items():
                if line == node.lineno and base <= start <= base + len(node.value):
                    lo = start - base
                    length = t.entry.end - t.entry.start
                    node.value = node.value[:lo] + t.spec + node.value[lo + length :]
    if ast.dump(a) != ast.dump(b):
        return "the code changed outside the edited strings"
    return None


def _check_setup_cfg(old: str, new: str) -> str | None:
    a, b = (
        configparser.ConfigParser(interpolation=None),
        configparser.ConfigParser(interpolation=None),
    )
    a.read_string(old)
    b.read_string(new)
    if a.sections() != b.sections():
        return "the sections changed"
    for section in a.sections():
        if dict(a.items(section)).keys() != dict(b.items(section)).keys():
            return f"the options of [{section}] changed"
        for option, value in a.items(section):
            dependency = (section, option) == ("options", "install_requires") or (
                section == "options.extras_require"
            )
            if not dependency and b.get(section, option) != value:
                return f"[{section}] {option} changed"
    return None


# --- report notes -----------------------------------------------------------------------------


def _conda_notes(path: str, text: str, migration: Migration) -> list[str]:
    """Conda entries of companion packages that hold the library back (never edited)."""
    if PurePosixPath(path).name.lower() not in CONDA_ENVIRONMENT_FILES:
        return []
    notes = []
    for spec, line, _ in parse_conda_environment(text).conda:
        name = conda_package_name(spec)
        pin = migration.companion_pins.get(name or "")
        m = re.match(r"^[^=<>!~\s]+\s*(==|=|>=|<=|<|>)?\s*([0-9][0-9.]*)", spec.split("::")[-1])
        if pin is None or not m:
            continue
        op = {"=": "==", None: "=="}.get(m.group(1), m.group(1))
        if _excludes(((op, m.group(2).rstrip(".")),), pin):
            notes.append(
                f"not changed: {path}:{line} {spec} (conda entry); {migration.package} needs "
                f"{name}{_companion_spec(((op, m.group(2)),), pin)}"
            )
    return notes


def _lockfile_notes(root: Path, path: str) -> list[str]:
    p = PurePosixPath(path)
    out = []
    for lock in LOCKFILES.get(p.name.lower(), ()):
        rel = (p.parent / lock).as_posix()
        if (root / rel).is_file():
            out.append(f"not updated: {rel}; regenerate it")
    return out
