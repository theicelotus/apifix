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


def _clause_allows(op: str, spec: str, v: Version) -> bool:
    if spec.endswith(".*") and op in {"==", "===", "!="}:
        try:
            prefix = tuple(int(x) for x in spec[:-2].lstrip("v").split("."))
        except ValueError:
            return True
        padded = tuple(v) + (0,) * max(0, len(prefix) - len(v))
        match = padded[: len(prefix)] == prefix
        return match if op != "!=" else not match
    s = parse_version(spec)
    if s is None:
        return True
    if op in {"==", "==="}:
        return v == s
    if op == "!=":
        return v != s
    if op == "<":
        return v < s
    if op == "<=":
        return v <= s
    if op == ">":
        return v > s
    if op == ">=":
        return v >= s
    if op == "~=":
        n = len(spec.split("."))
        return v >= s and (n < 2 or v < _bump(s, n - 2))
    return True


def allows(clauses: Iterable[tuple[str, str]], version: str) -> bool:
    """True if ``version`` satisfies every clause."""
    v = parse_version(version)
    return v is not None and all(_clause_allows(op, spec, v) for op, spec in clauses)


def pep440_clauses(spec: str) -> tuple[tuple[str, str], ...]:
    return _pep440_clauses(spec)


def poetry_clauses(spec: str) -> tuple[tuple[str, str], ...]:
    return _poetry_clauses(spec)


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
            for const in _string_literals(kw.value):
                dep = _from_requirement(const.value, const.lineno, const.col_offset + 2)
                if dep:
                    deps.append(dep)
    return deps


def _string_literals(node: ast.AST) -> Iterator[ast.Constant]:
    """String constants in a list/tuple/dict literal, skipping dict keys (extras names)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    elif isinstance(node, ast.List | ast.Tuple | ast.Set):
        for elt in node.elts:
            yield from _string_literals(elt)
    elif isinstance(node, ast.Dict):
        for value in node.values:
            yield from _string_literals(value)


# --- installable requirement strings ---------------------------------------------------------
#
# Unlike DeclaredDependency (for reporting), these keep extras and environment markers so pip
# can install them. References to the repo's own code (local paths, editables) are dropped:
# the repo is only ever installed offline, in the test phase.


def is_local_reference(req: str) -> bool:
    req = req.strip()
    if req.startswith((".", "/", "~", "file:")):
        return True
    return " @ file:" in req or "@file:" in req


def _strip_comment(line: str) -> str:
    return re.sub(r"(^|\s)#.*$", "", line).strip()


def requirements_from_file(relpath: str, text: str) -> list[str]:
    """PEP 508 requirement strings declared by one dependency file (any supported format)."""
    name = PurePosixPath(relpath).name.lower()
    if name == "pyproject.toml":
        return _pyproject_requirements(tomllib.loads(text))
    if name == "pipfile":
        data = tomllib.loads(text)
        return [
            r
            for section in ("packages", "dev-packages")
            for r in _table_requirements(data.get(section))
        ]
    if name == "setup.cfg":
        return _setup_cfg_requirements(text)
    if name == "setup.py":
        return _setup_py_requirements(text)
    return _requirements_txt(text)


def build_system_requires(pyproject_text: str | None, has_setup: bool) -> list[str]:
    """[build-system] requires, or the PEP 517 setuptools default for setup.py projects."""
    if pyproject_text is not None:
        build = tomllib.loads(pyproject_text).get("build-system", {})
        if "requires" in build:
            reqs = [r for r in build["requires"] if isinstance(r, str)]
            if build.get("build-backend", "").startswith("hatchling"):
                reqs.append("editables")  # used by hatchling for some editable modes
            return reqs
    return ["setuptools", "wheel"] if has_setup or pyproject_text is not None else []


def _requirements_txt(text: str) -> list[str]:
    out = []
    for line in text.replace("\\\n", " ").splitlines():
        line = _strip_comment(line)
        if not line or line.startswith("-") or is_local_reference(line):
            continue  # options, -r/-c/-e includes, local paths
        out.append(line)
    return out


def _pyproject_requirements(data: dict) -> list[str]:
    project = data.get("project", {})
    reqs = [r for r in project.get("dependencies", []) if isinstance(r, str)]
    for group in project.get("optional-dependencies", {}).values():
        reqs += [r for r in group if isinstance(r, str)]
    for group in data.get("dependency-groups", {}).values():
        reqs += [r for r in group if isinstance(r, str)]  # {include-group = ...} is skipped
    poetry = data.get("tool", {}).get("poetry", {})
    reqs += _table_requirements(poetry.get("dependencies"))
    reqs += _table_requirements(poetry.get("dev-dependencies"))
    for group in poetry.get("group", {}).values():
        reqs += _table_requirements(group.get("dependencies"))
    return [r for r in reqs if not is_local_reference(r)]


def _table_requirements(table: object) -> list[str]:
    """Poetry / Pipfile `name = spec` tables -> PEP 508 strings."""
    if not isinstance(table, dict):
        return []
    out = []
    for key, value in table.items():
        if key.lower() == "python":
            continue
        extras: list[str] = []
        markers = ""
        if isinstance(value, dict):
            if any(k in value for k in ("path", "git", "url", "file", "editable")):
                continue  # local code or VCS checkout
            extras = [e for e in value.get("extras", []) if isinstance(e, str)]
            markers = value.get("markers", "") if isinstance(value.get("markers"), str) else ""
            value = value.get("version", "*")
        if not isinstance(value, str):
            continue
        clauses = _poetry_clauses(value)  # also handles Pipfile's PEP 440 strings
        req = key + (f"[{','.join(extras)}]" if extras else "")
        req += ",".join(op + v for op, v in clauses)
        out.append(f"{req}; {markers}" if markers else req)
    return out


def _setup_cfg_requirements(text: str) -> list[str]:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    blocks = [
        cfg.get("options", key)
        for key in ("install_requires", "setup_requires", "tests_require")
        if cfg.has_option("options", key)
    ]
    if cfg.has_section("options.extras_require"):
        blocks += [v for _, v in cfg.items("options.extras_require")]
    reqs = [_strip_comment(line) for block in blocks for line in block.splitlines()]
    return [r for r in reqs if r and not is_local_reference(r)]


def _setup_py_requirements(text: str) -> list[str]:
    keys = {"install_requires", "extras_require", "tests_require", "setup_requires"}
    out = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in keys:
                    out += [c.value for c in _string_literals(kw.value)]
    return [r for r in out if not is_local_reference(r)]
