"""Find declared dependencies in requirements files, pyproject.toml (incl. pixi tables),
pixi.toml, setup.py/cfg, Pipfile and conda environment files (their pip: list).

Generic across migrations: it reports every dependency with its version constraints normalised
to PEP 440-style clauses; a migration decides which ones matter. Stdlib only (conda
environment files are read with a parser for the YAML subset they use).
"""

from __future__ import annotations

import ast
import configparser
import platform
import re
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

Version = tuple[int, ...]

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CLAUSE_RE = re.compile(r"^\s*(===|==|!=|~=|<=|>=|<|>)\s*([^\s,]+)\s*$")
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class DeclaredDependency:
    name: str  # normalised: lower case, "_" and "." -> "-"
    raw: str  # the entry as written, for reports
    clauses: tuple[tuple[str, str], ...]  # e.g. (("==", "0.28.1"),); empty = unconstrained
    line: int
    col: int
    # a direct reference (`name @ url`, a git / path / url table): no version to compare or set
    direct: bool = False


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


CONDA_ENVIRONMENT_FILES = ("environment.yml", "environment.yaml")


@dataclass(frozen=True)
class NotInstalled:
    """A declared dependency the install plan leaves out on purpose."""

    name: str  # normalised
    source: str  # where it is declared, e.g. "pyproject.toml [tool.pixi.dependencies]"
    why: str  # e.g. "conda-only package; not supported yet"


def is_dependency_file(relpath: str) -> bool:
    p = PurePosixPath(relpath)
    name = p.name.lower()
    if name in {"pyproject.toml", "pixi.toml", "setup.py", "setup.cfg", "pipfile"}:
        return True
    if name in CONDA_ENVIRONMENT_FILES:
        return True
    if p.suffix.lower() in {".txt", ".in"}:
        stem = p.stem.lower()
        return "requirements" in stem or "constraints" in stem or p.parent.name == "requirements"
    return False


_STRUCTURED_FILES = {"pyproject.toml", "setup.py", "setup.cfg", "pipfile"}
_STRICT_CLAUSE_RE = re.compile(r"^\s*(===|==|!=|~=|<=|>=|<|>)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*\s*$")


def dependency_file_problem(relpath: str, text: str) -> str | None:
    """Why `text` is not a well-formed dependency file of its kind, or None. Stricter than
    parse_dependency_file for requirements files, which it reads leniently: there every line
    must be blank, a comment, an option (``-r``, ``-e``, ...), a URL, or a valid requirement."""
    try:
        parse_dependency_file(relpath, text)
    except (ValueError, SyntaxError, configparser.Error) as exc:  # TOMLDecodeError: ValueError
        return f"{type(exc).__name__}: {exc}"
    name = PurePosixPath(relpath).name.lower()
    if name in {"pyproject.toml", "pixi.toml"}:
        root = _pixi_root(tomllib.loads(text), name)
        return _pixi_problem(name, root) if root is not None else None
    if name in {"setup.py", "setup.cfg", "pipfile"}:
        return None
    if name in CONDA_ENVIRONMENT_FILES:
        return _conda_environment_problem(text)
    for i, line in enumerate(text.splitlines(), 1):
        if not _valid_requirement_line(line):
            return f"line {i} is not a valid requirement: {line.strip()!r}"
    return None


def _valid_requirement_line(line: str) -> bool:
    """A requirements-file line that is blank, a comment, an option, a URL, or a requirement
    with strictly valid version clauses."""
    req = _strip_comment(line).split(";", 1)[0].strip()
    if not req or req.startswith(("-", "git+", "http:", "https:", "file:")):
        return True
    m = _NAME_RE.match(req)
    spec = m.group(3).strip() if m else ""
    return m is not None and (
        not spec
        or spec.startswith("@")
        or all(_STRICT_CLAUSE_RE.match(part) for part in spec.split(","))
    )


# --- a user's requirements file for the sandbox (--sandbox-requirements) --------------------

SANDBOX_REQUIREMENT_OPTIONS = ("--index-url", "--extra-index-url", "-i")
_CREDENTIALS_RE = re.compile(r"://[^/\s@]+@")


def sandbox_requirements_problem(text: str) -> str | None:
    """Why a user's requirements file cannot replace apifix's dependency detection, or None.
    Its lines reach the networked install container, so no includes, editables or local paths
    (they would not exist there), and no URL may carry credentials."""
    found = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        if _CREDENTIALS_RE.search(line):
            return f"line {i}: URLs with credentials are not allowed (the file is sent to pip)"
        if line.startswith("-"):
            option, _, value = line.replace("=", " ", 1).partition(" ")
            if option not in SANDBOX_REQUIREMENT_OPTIONS:
                return (
                    f"line {i}: option {option} is not allowed (only --index-url and "
                    "--extra-index-url)"
                )
            if not value.strip().startswith(("https://", "http://")):
                return f"line {i}: {option} needs an http(s) URL"
            continue
        if is_local_reference(line) or line.endswith("\\"):
            return f"line {i}: local paths and continuation lines are not allowed: {raw.strip()!r}"
        if not _valid_requirement_line(line):
            return f"line {i} is not a valid requirement: {raw.strip()!r}"
        found = True
    return None if found else "no requirements in the file"


def sandbox_requirement_lines(text: str) -> list[str]:
    """The lines of a checked --sandbox-requirements file that pip needs (comments dropped)."""
    return [line for line in map(_strip_comment, text.splitlines()) if line]


# --- import names -> distributions ----------------------------------------------------------

# Top-level import names whose distribution is called something else (normalised names).
IMPORT_DISTRIBUTIONS: dict[str, tuple[str, ...]] = {
    "attr": ("attrs",),
    "bs4": ("beautifulsoup4",),
    "crypto": ("pycryptodome",),
    "cv2": ("opencv-python", "opencv-python-headless"),
    "dateutil": ("python-dateutil",),
    "docx": ("python-docx",),
    "dotenv": ("python-dotenv",),
    "git": ("gitpython",),
    "jwt": ("pyjwt",),
    "magic": ("python-magic",),
    "openssl": ("pyopenssl",),
    "pil": ("pillow",),
    "pptx": ("python-pptx",),
    "serial": ("pyserial",),
    "sklearn": ("scikit-learn",),
    "speech-recognition": ("speechrecognition",),
    "usb": ("pyusb",),
    "whisper": ("openai-whisper",),
    "yaml": ("pyyaml",),
}


def distribution_candidates(module: str) -> tuple[str, ...]:
    """Normalised distribution names that may provide the top-level module `module`: the
    module's own name first, then known differently named distributions."""
    name = normalize_name(module.split(".", 1)[0])
    return (name, *IMPORT_DISTRIBUTIONS.get(name, ()))


def parse_dependency_file(relpath: str, text: str) -> list[DeclaredDependency]:
    """Parse one dependency file. Raises ValueError/SyntaxError on malformed input."""
    name = PurePosixPath(relpath).name.lower()
    if name == "pyproject.toml":
        return _parse_pyproject(text)
    if name == "pixi.toml":
        return list(_pixi_declared(text, tomllib.loads(text), name))
    if name in CONDA_ENVIRONMENT_FILES:
        env = parse_conda_environment(text)
        return [d for req, line, col in env.pip if (d := _from_requirement(req, line, col))]
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


def _lower_bound(op: str, version: str) -> Version | None:
    """The lowest version one clause allows (inclusive for `>`: every allowed version is above
    it), or None if the clause has no lower bound."""
    if op in {"==", "==="} and version.endswith(".*"):
        return parse_version(version[:-2])
    if op in {"==", "===", ">=", ">", "~="}:
        return parse_version(version)
    return None


def entirely_at_or_above(clauses: Iterable[tuple[str, str]], bound: str) -> bool:
    """True if every version allowed by ``clauses`` is ``bound`` or higher (unconstrained:
    False)."""
    limit = parse_version(bound)
    if limit is None:
        return False
    for op, version in clauses:
        lb = _lower_bound(op, version)
        if lb is not None and lb >= limit:
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


_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")


def _table_span(lines: list[str], table: str) -> tuple[int, int] | None:
    """Line indexes [start, end) of the body of the TOML table `[table]` (dotted name, quotes
    and spaces ignored), or None if the file has no such header."""
    start = None
    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if line.lstrip().startswith("[") and start is not None:
            return start, i
        if m and re.sub(r"[\s\"']", "", m.group(1)) == table:
            start = i + 1
    return (start, len(lines)) if start is not None else None


def _locate_key(text: str, key: str, table: str | None = None) -> tuple[int, int]:
    """Line and column of `key = ...`, searched in the body of `[table]` when the file has that
    header (otherwise, e.g. for inline tables, in the whole file)."""
    pattern = re.compile(rf"^(\s*)[\"']?{re.escape(key)}[\"']?\s*=", re.IGNORECASE)
    lines = text.splitlines()
    span = _table_span(lines, table) if table else None
    for start, end in ([span] if span else []) + [(0, len(lines))]:
        for i in range(start, end):
            m = pattern.match(lines[i])
            if m:
                return i + 1, len(m.group(1)) + 1
    return 1, 1


def _from_requirement(req: str, line: int, col: int) -> DeclaredDependency | None:
    req = req.split("#", 1)[0].split(";", 1)[0].strip()
    if not req or req.startswith(("-", "git+", "http:", "https:", "file:")):
        return None
    m = _NAME_RE.match(req)
    if not m:
        return None
    spec = m.group(3).strip()
    direct = spec.startswith("@")  # direct URL reference
    return DeclaredDependency(
        normalize_name(m.group(1)), req, () if direct else _pep440_clauses(spec), line, col, direct
    )


def _from_requirement_list(text: str, reqs: Iterable[object]) -> Iterator[DeclaredDependency]:
    for req in reqs:
        if isinstance(req, str):
            line, col = _locate(text, req)
            dep = _from_requirement(req, line, col)
            if dep:
                yield dep


# --- per-format parsers ---------------------------------------------------------------------


# per-requirement options after a requirement (`--hash=...`, `--config-settings ...`)
REQUIREMENT_OPTIONS = re.compile(r"\s+--[A-Za-z].*$")


def requirement_line(line: str) -> str:
    """A requirements-file line without its comment, a trailing `\\` (continuation) and
    per-requirement options, so that `openai==0.28.1 \\` (hashes on the next lines) and
    `openai==0.28.1 --hash=sha256:...` still read as `openai==0.28.1`."""
    req = _strip_comment(line).removesuffix("\\").rstrip()
    return req if req.startswith("-") else REQUIREMENT_OPTIONS.sub("", req)


def _parse_requirements(text: str) -> list[DeclaredDependency]:
    deps = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = requirement_line(line)
        dep = _from_requirement(stripped, i, len(line) - len(line.lstrip()) + 1)
        if dep:
            deps.append(dep)
    return deps


# keys of a Poetry / Pipfile / pixi table entry that make it a direct reference
_DIRECT_KEYS = ("git", "path", "url", "file", "hg", "svn", "bzr")


def _poetry_table(
    text: str, table: object, header: str | None = None
) -> Iterator[DeclaredDependency]:
    """`name = spec` tables (Poetry, Pipfile, pixi); `header` is the table's dotted name, to
    find each entry's line inside it."""
    if not isinstance(table, dict):
        return
    for key, value in table.items():
        direct = False
        if isinstance(value, dict):
            direct = "version" not in value and any(k in value for k in _DIRECT_KEYS)
            value = value.get("version", "")
        if not isinstance(value, str):
            continue
        line, col = _locate_key(text, key, header)
        raw = f"{key} = {value!r}" if value else key
        yield DeclaredDependency(
            normalize_name(key), raw, _poetry_clauses(value), line, col, direct
        )


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
    deps += _poetry_table(text, poetry.get("dependencies"), "tool.poetry.dependencies")
    deps += _poetry_table(text, poetry.get("dev-dependencies"), "tool.poetry.dev-dependencies")
    for name, group in poetry.get("group", {}).items():
        deps += _poetry_table(
            text, group.get("dependencies"), f"tool.poetry.group.{name}.dependencies"
        )
    deps += _pixi_declared(text, data, "pyproject.toml")
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


# --- pixi -------------------------------------------------------------------------------------
#
# pixi manifests: [tool.pixi] in pyproject.toml, or the top level of pixi.toml. Their
# `pypi-dependencies` tables are pip requirements; `dependencies` tables are conda packages
# (not installed: conda-only packages are not supported yet). Both exist at the top, per
# `target.<platform>` and per `feature.<name>` (which has targets too).

PIXI_PYPI = "pypi-dependencies"
PIXI_CONDA = "dependencies"
_PIXI_TABLE_KEYS = {
    "version",
    "extras",
    "path",
    "editable",
    "git",
    "branch",
    "tag",
    "rev",
    "subdirectory",
    "url",
    "index",
    "env-markers",
}


def _pixi_root(data: dict, name: str) -> dict | None:
    if name == "pixi.toml":
        return data
    pixi = data.get("tool", {}).get("pixi")
    return pixi if isinstance(pixi, dict) else None


def has_pixi_manifest(pyproject_text: str | None, has_pixi_toml: bool) -> bool:
    if has_pixi_toml:
        return True
    if pyproject_text is None:
        return False
    try:
        return _pixi_root(tomllib.loads(pyproject_text), "pyproject.toml") is not None
    except ValueError:
        return False


@dataclass(frozen=True)
class _PixiTable:
    header: str  # dotted TOML name, e.g. "tool.pixi.target.osx-arm64.pypi-dependencies"
    kind: str  # PIXI_PYPI or PIXI_CONDA
    target: str | None  # the platform selector, None for every platform
    table: dict


def _pixi_tables(root: dict, prefix: str) -> Iterator[_PixiTable]:
    def dotted(*parts: str) -> str:
        return ".".join(p for p in (prefix, *parts) if p)

    def level(base: tuple[str, ...], node: dict) -> Iterator[_PixiTable]:
        for kind in (PIXI_PYPI, PIXI_CONDA):
            if isinstance(node.get(kind), dict):
                yield _PixiTable(dotted(*base, kind), kind, None, node[kind])
        targets = node.get("target")
        for target, sub in targets.items() if isinstance(targets, dict) else ():
            for kind in (PIXI_PYPI, PIXI_CONDA):
                if isinstance(sub, dict) and isinstance(sub.get(kind), dict):
                    yield _PixiTable(dotted(*base, "target", target, kind), kind, target, sub[kind])

    yield from level((), root)
    features = root.get("feature")
    for name, feature in features.items() if isinstance(features, dict) else ():
        if isinstance(feature, dict):
            yield from level(("feature", name), feature)


def _pixi_prefix(name: str) -> str:
    return "" if name == "pixi.toml" else "tool.pixi"


def _pixi_declared(text: str, data: dict, name: str) -> Iterator[DeclaredDependency]:
    """pypi-dependencies entries of every table (all platforms: a stale pin is stale anywhere)."""
    root = _pixi_root(data, name)
    if root is None:
        return
    for t in _pixi_tables(root, _pixi_prefix(name)):
        if t.kind == PIXI_PYPI:
            yield from _poetry_table(text, t.table, t.header)


def sandbox_pixi_targets(machine: str | None = None) -> frozenset[str]:
    """pixi platform selectors that apply in the sandbox (Linux, the host's architecture)."""
    arch = (machine or platform.machine()).lower()
    arch = {"x86_64": "64", "amd64": "64", "arm64": "aarch64"}.get(arch, arch)
    return frozenset({"linux", "unix", f"linux-{arch}"})


def _pixi_problem(name: str, root: dict) -> str | None:
    """Strict checks on pixi pypi-dependencies: each value is "*", PEP 440 clauses, or a
    table with known keys and such a version."""
    for t in _pixi_tables(root, _pixi_prefix(name)):
        if t.kind != PIXI_PYPI:
            continue
        for key, value in t.table.items():
            where = f"{name} [{t.header}] {key}"
            if not _PACKAGE_NAME_RE.match(key):
                return f"{where}: not a valid package name"
            if isinstance(value, dict):
                unknown = sorted(set(value) - _PIXI_TABLE_KEYS)
                if unknown:
                    return f"{where}: unknown key(s) {', '.join(unknown)}"
                value = value.get("version", "*")
            if not isinstance(value, str) or not _valid_version_spec(value):
                return f"{where}: invalid version {value!r}"
    return None


def _valid_version_spec(spec: str) -> bool:
    spec = spec.strip()
    return spec == "*" or all(_STRICT_CLAUSE_RE.match(part) for part in spec.split(","))


# --- conda environment files (environment.yml) ----------------------------------------------

_YAML_KEY_RE = re.compile(r"^([A-Za-z_][\w.-]*)\s*:(?:\s+(.*))?$")
_UNSUPPORTED_YAML = ("[", "{", "&", "*", "!", "|", ">", "%", "@", "`")
_CONDA_SPEC_RE = re.compile(
    r"^(?:[\w./:-]+::)?([A-Za-z0-9_][A-Za-z0-9._-]*)\s*([\w.*+!<>=,|~ \[\]'\"-]*)$"
)
_CONDA_BUILD_RE = re.compile(r"^[^=<>!~\s]+=[^=\s]+=[^=\s]+$")  # name=version=build


@dataclass(frozen=True)
class CondaEnvironment:
    conda: tuple[tuple[str, int, int], ...]  # (match spec, line, col)
    pip: tuple[tuple[str, int, int], ...]  # (requirement line, line, col)
    exported: bool  # `conda env export` output: a prefix: key or build strings


def _yaml_strip_comment(text: str) -> str:
    quote = None
    for i, c in enumerate(text):
        if quote:
            quote = None if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _yaml_scalar(value: str, line: int) -> str:
    value = value.strip()
    if value.startswith(_UNSUPPORTED_YAML):
        raise ValueError(f"unsupported YAML at line {line}: {value!r}")
    if value[:1] in ("'", '"'):
        q = value[0]
        if len(value) < 2 or value[-1] != q:
            raise ValueError(f"unterminated quoted string at line {line}")
        inner = value[1:-1]
        return inner.replace("''", "'") if q == "'" else inner.replace('\\"', '"')
    return value


def parse_conda_environment(text: str) -> CondaEnvironment:
    """Read a conda environment file: the YAML subset conda writes and people write by hand
    (top-level keys, block lists, plain or quoted scalars, comments, one `- pip:` list).
    Anything else raises ValueError naming the line."""
    conda: list[tuple[str, int, int]] = []
    pip: list[tuple[str, int, int]] = []
    section: str | None = None
    prefix = False
    item_indent = pip_indent = None
    in_pip = False
    for n, raw in enumerate(text.splitlines(), 1):
        body = raw.lstrip(" ")
        if body.startswith("\t"):
            raise ValueError(f"tab indentation at line {n}")
        content = _yaml_strip_comment(body)
        if not content or (n == 1 and content == "---"):
            continue
        indent = len(raw) - len(body)
        is_item = content == "-" or content.startswith("- ")
        if indent == 0 and not is_item:
            m = _YAML_KEY_RE.match(content)
            if not m:
                raise ValueError(f"expected a top-level key at line {n}: {content!r}")
            section, value = m.group(1), m.group(2)
            prefix = prefix or section == "prefix"
            item_indent, in_pip = None, False
            if value:
                if section == "dependencies":
                    raise ValueError(f"dependencies must be a block list (line {n})")
                _yaml_scalar(value, n)
            continue
        if section is None:
            raise ValueError(f"content before any key at line {n}")
        if section != "dependencies":
            item = content[1:] if is_item else content
            m = _YAML_KEY_RE.match(item.strip())
            if m and m.group(2):
                _yaml_scalar(m.group(2), n)
            elif item.strip() and not m:
                _yaml_scalar(item, n)
            continue
        if not is_item:
            raise ValueError(f"expected a list item under dependencies at line {n}")
        item = content[1:].strip()
        col = indent + 3
        if in_pip and item_indent is not None and indent > item_indent:
            if pip_indent is None:
                pip_indent = indent
            elif indent != pip_indent:
                raise ValueError(f"inconsistent indentation in the pip list at line {n}")
            pip.append((_yaml_scalar(item, n), n, col))
            continue
        if item_indent is None:
            item_indent = indent
        elif indent != item_indent:
            raise ValueError(f"inconsistent indentation under dependencies at line {n}")
        in_pip = False
        if re.fullmatch(r"pip\s*:", item):
            in_pip, pip_indent = True, None
            continue
        if _YAML_KEY_RE.match(item):
            raise ValueError(f"unsupported mapping under dependencies at line {n}: {item!r}")
        spec = _yaml_scalar(item, n)
        if not spec:
            raise ValueError(f"empty list item at line {n}")
        conda.append((spec, n, col))
    exported = prefix or any(_CONDA_BUILD_RE.match(spec) for spec, _, _ in conda)
    return CondaEnvironment(tuple(conda), tuple(pip), exported)


def conda_package_name(spec: str) -> str | None:
    """The package a conda match spec names (normalised), or None if it is not one."""
    m = _CONDA_SPEC_RE.match(spec.strip())
    return normalize_name(m.group(1)) if m else None


def _conda_environment_problem(text: str) -> str | None:
    env = parse_conda_environment(text)
    for spec, line, _ in env.conda:
        if conda_package_name(spec) is None:
            return f"line {line} is not a valid conda package spec: {spec!r}"
    for req, line, _ in env.pip:
        if not _valid_requirement_line(req):
            return f"line {line} is not a valid pip requirement: {req!r}"
    return None


# --- what to install, with where each requirement comes from ---------------------------------

CONDA_ONLY = "conda-only package; not supported yet"
EXPORTED_CONDA = "exported conda environment (build strings or prefix:); not installed"
_ALWAYS_PRESENT = frozenset({"python", "pip"})  # conda entries the sandbox has anyway
# pip packages that only install on one OS; skipped on Linux when no marker says so
_PLATFORM_ONLY = (
    (re.compile(r"^(pyobjc(-.*)?|appnope)$"), "macOS only"),
    (re.compile(r"^(pywin32|pypiwin32|pywinpty|windows-curses)$"), "Windows only"),
)


def platform_only(name: str) -> str | None:
    """ "macOS only" / "Windows only" for packages known not to install on Linux, else None."""
    name = normalize_name(name)
    return next((why for pattern, why in _PLATFORM_ONLY if pattern.match(name)), None)


def install_requirements(
    relpath: str, text: str, targets: frozenset[str] | None = None
) -> tuple[list[tuple[str, str]], list[NotInstalled]]:
    """(requirement, where it is declared) pairs to pip-install from one dependency file, and
    the declared packages it leaves out (conda-only packages, other platforms' pixi targets).
    `targets`: the pixi platform selectors that apply (default: the sandbox's)."""
    name = PurePosixPath(relpath).name.lower()
    if name in CONDA_ENVIRONMENT_FILES:
        return _conda_environment_requirements(relpath, text)
    if name not in {"pyproject.toml", "pixi.toml"}:
        return [(req, relpath) for req in requirements_from_file(relpath, text)], []
    data = tomllib.loads(text)
    reqs = (
        [(req, relpath) for req in _pyproject_requirements(data)]
        if name == "pyproject.toml"
        else []
    )
    left_out: list[NotInstalled] = []
    root = _pixi_root(data, name)
    if root is None:
        return reqs, left_out
    targets = sandbox_pixi_targets() if targets is None else targets
    for t in _pixi_tables(root, _pixi_prefix(name)):
        source = f"{relpath} [{t.header}]"
        applies = t.target is None or t.target in targets
        if t.kind == PIXI_PYPI and applies:
            reqs += [(req, source) for req in _table_requirements(t.table)]
            continue
        why = CONDA_ONLY if applies else f"pixi target {t.target}; the sandbox runs Linux"
        left_out += [
            NotInstalled(normalize_name(key), source, why)
            for key in t.table
            if normalize_name(key) not in _ALWAYS_PRESENT
        ]
    return reqs, left_out


def _conda_environment_requirements(
    relpath: str, text: str
) -> tuple[list[tuple[str, str]], list[NotInstalled]]:
    env = parse_conda_environment(text)
    pip_lines = "\n".join(req for req, _, _ in env.pip)
    reqs = [(req, f"{relpath} (pip)") for req in _requirements_txt(pip_lines)]
    why = EXPORTED_CONDA if env.exported else CONDA_ONLY
    left_out = [
        NotInstalled(name, f"{relpath} (conda)", why)
        for spec, _, _ in env.conda
        if (name := conda_package_name(spec)) and name not in _ALWAYS_PRESENT
    ]
    return reqs, left_out


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
    if name in {"pyproject.toml", "pixi.toml"} or name in CONDA_ENVIRONMENT_FILES:
        return [req for req, _ in install_requirements(relpath, text)[0]]
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
