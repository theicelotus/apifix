"""Find declared dependencies in requirements files, pyproject.toml, setup.py/cfg and Pipfile.

Generic across migrations: it reports every dependency with its version constraints normalised
to PEP 440-style clauses; a migration decides which ones matter. Stdlib only.
"""

from __future__ import annotations

import ast
import configparser
import re
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

Version = tuple[int, ...]

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_CLAUSE_RE = re.compile(r"^\s*(===|==|!=|~=|<=|>=|<|>)\s*([^\s,]+)\s*$")
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class DeclaredDependency:
    name: str  # normalised: lower case, "_" and "." -> "-"
    raw: str  # the entry as written, for reports
    clauses: tuple[tuple[str, str], ...]  # e.g. (("==", "0.28.1"),); empty = unconstrained
    line: int
    col: int


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_dependency_file(relpath: str) -> bool:
    p = PurePosixPath(relpath)
    name = p.name.lower()
    if name in {"pyproject.toml", "setup.py", "setup.cfg", "pipfile"}:
        return True
    if p.suffix.lower() in {".txt", ".in"}:
        stem = p.stem.lower()
        return "requirements" in stem or "constraints" in stem or p.parent.name == "requirements"
    return False


def parse_dependency_file(relpath: str, text: str) -> list[DeclaredDependency]:
    """Parse one dependency file. Raises ValueError/SyntaxError on malformed input."""
    name = PurePosixPath(relpath).name.lower()
    if name == "pyproject.toml":
        return _parse_pyproject(text)
    if name == "pipfile":
        return _parse_pipfile(text)
    if name == "setup.cfg":
        return _parse_setup_cfg(text)
    if name == "setup.py":
        return _parse_setup_py(text)
    return _parse_requirements(text)


# --- version constraint helpers -------------------------------------------------------------


def parse_version(text: str) -> Version | None:
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _bump(v: Version, index: int) -> Version:
    """Smallest version greater than everything starting with v[: index + 1]."""
    padded = list(v) + [0] * (index + 1 - len(v))
    return tuple(padded[:index] + [padded[index] + 1])


def _upper_bound(op: str, version: str) -> tuple[Version, bool] | None:
    """(bound, inclusive) implied by one clause, or None if the clause has no upper bound."""
    if version.endswith(".*") and op in {"==", "==="}:
        v = parse_version(version[:-2])
        return (_bump(v, len(version[:-2].split(".")) - 1), False) if v else None
    v = parse_version(version)
    if v is None:
        return None
    if op in {"==", "===", "<="}:
        return v, True
    if op == "<":
        return v, False
    if op == "~=":
        n = len(version.split("."))
        return (_bump(v, n - 2), False) if n >= 2 else None
    return None


def entirely_below(clauses: Iterable[tuple[str, str]], bound: str) -> bool:
    """True if every version allowed by ``clauses`` is lower than ``bound``."""
    limit = parse_version(bound)
    if limit is None:
        return False
    for op, version in clauses:
        ub = _upper_bound(op, version)
        if ub is None:
            continue
        v, inclusive = ub
        if v < limit or (not inclusive and v <= limit):
            return True
    return False


def _pep440_clauses(spec: str) -> tuple[tuple[str, str], ...]:
    out = []
    for part in spec.split(","):
        m = _CLAUSE_RE.match(part)
        if m:
            out.append((m.group(1), m.group(2)))
    return tuple(out)


def _poetry_clauses(spec: str) -> tuple[tuple[str, str], ...]:
    """Translate a Poetry/Pipfile version string into PEP 440 clauses."""
    spec = spec.strip()
    if not spec or spec == "*" or "||" in spec:
        return ()
    out: list[tuple[str, str]] = []
    for part in spec.split(","):
        part = part.strip()
        if part.startswith(("^", "~")) and not part.startswith("~="):
            v = parse_version(part[1:])
            if v is None:
                continue
            n = len(part[1:].split("."))
            if part[0] == "^":
                idx = next((i for i, x in enumerate(v) if x != 0), max(n - 1, 0))
                idx = min(idx, n - 1)
            else:
                idx = 1 if n >= 2 else 0
            out += [(">=", part[1:]), ("<", ".".join(map(str, _bump(v, idx))))]
        elif _CLAUSE_RE.match(part):
            out += _pep440_clauses(part)
        elif parse_version(part):
            out.append(("==", part))
    return tuple(out)


# --- locating entries in source text --------------------------------------------------------


def _locate(text: str, needle: str) -> tuple[int, int]:
    for i, line in enumerate(text.splitlines(), 1):
        col = line.find(needle)
        if col != -1:
            return i, col + 1
    return 1, 1


def _locate_key(text: str, key: str) -> tuple[int, int]:
    pattern = re.compile(rf"^(\s*)[\"']?{re.escape(key)}[\"']?\s*=", re.IGNORECASE)
    for i, line in enumerate(text.splitlines(), 1):
        m = pattern.match(line)
        if m:
            return i, len(m.group(1)) + 1
    return 1, 1


def _from_requirement(req: str, line: int, col: int) -> DeclaredDependency | None:
    req = req.split("#", 1)[0].split(";", 1)[0].strip()
    if not req or req.startswith(("-", "git+", "http:", "https:", "file:")):
        return None
    m = _NAME_RE.match(req)
    if not m:
        return None
    spec = m.group(3).strip()
    if spec.startswith("@"):  # direct URL reference
        spec = ""
    return DeclaredDependency(normalize_name(m.group(1)), req, _pep440_clauses(spec), line, col)


def _from_requirement_list(text: str, reqs: Iterable[object]) -> Iterator[DeclaredDependency]:
    for req in reqs:
        if isinstance(req, str):
            line, col = _locate(text, req)
            dep = _from_requirement(req, line, col)
            if dep:
                yield dep


# --- per-format parsers ---------------------------------------------------------------------


def _parse_requirements(text: str) -> list[DeclaredDependency]:
    deps = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        dep = _from_requirement(stripped, i, len(line) - len(line.lstrip()) + 1)
        if dep:
            deps.append(dep)
    return deps


def _poetry_table(text: str, table: object) -> Iterator[DeclaredDependency]:
    if not isinstance(table, dict):
        return
    for key, value in table.items():
        if isinstance(value, dict):
            value = value.get("version", "")
        if not isinstance(value, str):
            continue
        line, col = _locate_key(text, key)
        raw = f"{key} = {value!r}" if value else key
        yield DeclaredDependency(normalize_name(key), raw, _poetry_clauses(value), line, col)


def _parse_pyproject(text: str) -> list[DeclaredDependency]:
    data = tomllib.loads(text)
    deps: list[DeclaredDependency] = []
    project = data.get("project", {})
    deps += _from_requirement_list(text, project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        deps += _from_requirement_list(text, group)
    for group in data.get("dependency-groups", {}).values():
        deps += _from_requirement_list(text, group)
    poetry = data.get("tool", {}).get("poetry", {})
    deps += _poetry_table(text, poetry.get("dependencies"))
    deps += _poetry_table(text, poetry.get("dev-dependencies"))
    for group in poetry.get("group", {}).values():
        deps += _poetry_table(text, group.get("dependencies"))
    return deps


def _parse_pipfile(text: str) -> list[DeclaredDependency]:
    data = tomllib.loads(text)
    deps: list[DeclaredDependency] = []
    for section in ("packages", "dev-packages"):
        deps += _poetry_table(text, data.get(section))
    return deps


def _parse_setup_cfg(text: str) -> list[DeclaredDependency]:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    blocks = []
    if cfg.has_option("options", "install_requires"):
        blocks.append(cfg.get("options", "install_requires"))
    if cfg.has_section("options.extras_require"):
        blocks += [v for _, v in cfg.items("options.extras_require")]
    reqs = [line.strip() for block in blocks for line in block.splitlines() if line.strip()]
    return list(_from_requirement_list(text, reqs))


def _parse_setup_py(text: str) -> list[DeclaredDependency]:
    tree = ast.parse(text)
    deps = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg not in {"install_requires", "extras_require", "tests_require"}:
                continue
            for const in ast.walk(kw.value):
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    dep = _from_requirement(const.value, const.lineno, const.col_offset + 2)
                    if dep:
                        deps.append(dep)
    return deps
