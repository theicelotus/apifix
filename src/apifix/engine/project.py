"""Facts about a target repo that the sandbox needs: Python version, test runner, dependencies.

Everything here reads files only; nothing from the repo is imported or executed.
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from apifix.engine.dependencies import (
    CONDA_ENVIRONMENT_FILES,
    NotInstalled,
    allows,
    build_system_requires,
    has_pixi_manifest,
    install_requirements,
    is_dependency_file,
    normalize_name,
    pep440_clauses,
    platform_only,
    poetry_clauses,
    requirements_from_file,
    sandbox_requirement_lines,
    sandbox_requirements_problem,
)
from apifix.engine.walk import iter_files

SUPPORTED_PYTHONS = ("3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14")
DEFAULT_PYTHON = "3.11"

Runner = Literal["pytest", "unittest"]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


# --- Python version -------------------------------------------------------------------------


@dataclass(frozen=True)
class PythonChoice:
    version: str  # "3.11"
    source: str  # where it came from, for the report


_MINOR_RE = re.compile(r"(?<![\d.])(3)\.(\d{1,2})(?![\d])")


def _minors(text: str) -> list[str]:
    return [f"{a}.{b}" for a, b in _MINOR_RE.findall(text)]


def _python_version_file(repo: Path) -> str | None:
    text = _read(repo / ".python-version")
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            m = re.match(r"^(?:cpython-|python-?)?(3\.\d{1,2})(?:\.|$)", line)
            return m.group(1) if m else None
    return None


def _runtime_txt(repo: Path) -> str | None:
    m = re.match(r"^\s*python-(3\.\d{1,2})", _read(repo / "runtime.txt") or "")
    return m.group(1) if m else None


def _ci_versions(repo: Path) -> list[tuple[str, str]]:
    """(version, workflow file) for every python-version listed in GitHub Actions workflows."""
    found = []
    files = _workflow_files(repo)
    key = re.compile(r"^(\s*)-?\s*python(?:-versions?)?\s*:\s*(.*)$")
    for wf in files:
        lines = (_read(wf) or "").splitlines()
        rel = wf.relative_to(repo).as_posix()
        for i, line in enumerate(lines):
            m = key.match(line)
            if not m:
                continue
            value = m.group(2).split("#", 1)[0].strip()
            if value:
                found += [(v, rel) for v in _minors(value)]
                continue
            for item in lines[i + 1 :]:  # block list: "- 3.9" lines
                im = re.match(r"^\s*-\s*(.+)$", item)
                if not im:
                    break
                found += [(v, rel) for v in _minors(im.group(1))]
    return found


def _workflow_files(repo: Path) -> list[Path]:
    workflows = repo / ".github" / "workflows"
    return sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml"))


def ci_env_vars(repo: Path, names: Iterable[str]) -> dict[str, str]:
    """Which of `names` a GitHub Actions workflow sets (name -> first workflow file): as a YAML
    key (`env:` blocks, `NAME: ${{ secrets.NAME }}`) or as `NAME=` in a step (`$GITHUB_ENV`,
    inline). Regex over the files, no YAML parser; comments are ignored."""
    found: dict[str, str] = {}
    wanted = [n for n in names if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)]
    for wf in _workflow_files(repo):
        rel = wf.relative_to(repo).as_posix()
        for line in (_read(wf) or "").splitlines():
            code = line.split("#", 1)[0]
            for name in wanted:
                if name in found:
                    continue
                if re.match(rf"^\s*-?\s*{name}\s*:", code) or re.search(
                    rf"(?<![\w$]){name}=", code
                ):
                    found[name] = rel
    return found


def _python_constraint(repo: Path) -> tuple[tuple[tuple[str, str], ...], str] | None:
    text = _read(repo / "pyproject.toml")
    if text:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        spec = data.get("project", {}).get("requires-python")
        if isinstance(spec, str):
            return pep440_clauses(spec), "pyproject.toml requires-python"
        spec = data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
        if isinstance(spec, str):
            return poetry_clauses(spec), "pyproject.toml [tool.poetry] python"
    text = _read(repo / "setup.cfg")
    if text:
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read_string(text)
            if cfg.has_option("options", "python_requires"):
                spec = cfg.get("options", "python_requires")
                return pep440_clauses(spec), "setup.cfg python_requires"
        except configparser.Error:
            pass
    text = _read(repo / "setup.py")
    if text:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "python_requires"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                return pep440_clauses(node.value.value), "setup.py python_requires"
    return None


def _allows_minor(clauses: tuple[tuple[str, str], ...], minor: str) -> bool:
    return allows(clauses, f"{minor}.0") or allows(clauses, f"{minor}.999")


def detect_python_version(repo: Path, default: str = DEFAULT_PYTHON) -> PythonChoice:
    """Explicit pins (.python-version, runtime.txt, CI) win if they satisfy the declared
    requires-python; otherwise the default if allowed, else the newest allowed version."""
    constraint = _python_constraint(repo)

    def ok(v: str) -> bool:
        return v in SUPPORTED_PYTHONS and (constraint is None or _allows_minor(constraint[0], v))

    explicit: list[tuple[str, str]] = []
    if v := _python_version_file(repo):
        explicit.append((v, ".python-version"))
    if v := _runtime_txt(repo):
        explicit.append((v, "runtime.txt"))
    ci = [(v, src) for v, src in _ci_versions(repo) if ok(v)]
    if ci:
        explicit.append(max(ci, key=lambda pair: _key(pair[0])))
    for v, src in explicit:
        if ok(v):
            return PythonChoice(v, src)

    if constraint is not None:
        clauses, src = constraint
        if ok(default):
            return PythonChoice(default, f"default (allowed by {src})")
        allowed = [v for v in SUPPORTED_PYTHONS if ok(v)]
        if allowed:
            return PythonChoice(max(allowed, key=_key), src)
    return PythonChoice(default, "default")


# --- tests ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TestPlan:
    __test__ = False  # not a pytest test class

    runner: Runner | None
    test_files: tuple[str, ...]  # the files the runner is given (or would collect)
    command: tuple[str, ...]  # "{python}" is replaced by the interpreter inside the sandbox
    reason: str
    # Repo-relative dirs the test phase puts on PYTHONPATH (tests that import sibling modules by
    # bare name, e.g. backend/testapp.py doing `from app import ...`), and why.
    import_paths: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# One definition of "test file", used by the scanner, the sandbox, the verifier (tampering
# checks) and whole-file mode. By name: unittest's discovery default `test*.py`, pytest's
# `*_test.py`, `conftest.py`, anything under a tests directory. By content: see defines_tests.
TEST_FILE_RE = re.compile(r"^(test.*|.*_test)\.py$")
TEST_DIRS = frozenset({"tests", "test", "testing"})
_TEST_MODULES = frozenset({"unittest", "pytest"})
MAX_TEST_SOURCE_BYTES = 1_000_000
_PYTEST_CONFIG_SECTIONS = {
    "pyproject.toml": re.compile(r"^\[tool\.pytest(\.ini_options)?\]", re.M),
    "setup.cfg": re.compile(r"^\[tool:pytest\]", re.M),
    "tox.ini": re.compile(r"^\[pytest\]", re.M),
}
_PYTEST_IMPORT = re.compile(r"^\s*(import pytest|from pytest\b)", re.M)

PYTEST_COMMAND = (
    "{python}",
    "-m",
    "pytest",
    "-p",
    "no:cacheprovider",
    "--junitxml=/results/junit.xml",
    "-rfE",
)


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def defines_tests(source: str) -> bool:
    """Imports unittest or pytest, and defines at the top level a class named Test* (or a
    *TestCase subclass) or a function named test_*."""
    if "unittest" not in source and "pytest" not in source:
        return False
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    imports = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = imports or any(a.name.split(".")[0] in _TEST_MODULES for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports = imports or node.module.split(".")[0] in _TEST_MODULES
    if not imports:
        return False
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and (
            node.name.startswith("Test")
            or any(_base_name(b).endswith("TestCase") for b in node.bases)
        ):
            return True
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test_"
        ):
            return True
    return False


def _test_name(path: PurePosixPath) -> bool:
    return bool(TEST_FILE_RE.match(path.name)) or path.name == "conftest.py"


def is_test_file(relpath: str, source: str | None = None, *, root: Path | None = None) -> bool:
    """Is `relpath` a test file: by name or directory, else by content (`source`, or the file
    read from `root` when no source is given)."""
    p = PurePosixPath(relpath)
    if p.suffix != ".py":
        return False
    if _test_name(p) or TEST_DIRS & set(p.parts[:-1]):
        return True
    if source is None and root is not None:
        source = _read_source(root / relpath)
    return source is not None and defines_tests(source)


def _read_source(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TEST_SOURCE_BYTES:
            return None
    except OSError:
        return None
    return _read(path)


def _pytest_signal(repo: Path, rel_files: list[str]) -> str | None:
    if (repo / "pytest.ini").is_file():
        return "pytest.ini"
    for name, pattern in _PYTEST_CONFIG_SECTIONS.items():
        if pattern.search(_read(repo / name) or ""):
            return f"{name} pytest config"
    if any(Path(f).name == "conftest.py" for f in rel_files):
        return "conftest.py"
    for rel in rel_files:
        if is_dependency_file(rel) and re.search(r"\bpytest\b", _read(repo / rel) or ""):
            return f"pytest declared in {rel}"
    return None


def pytest_config(repo: Path) -> tuple[str | None, dict[str, list[str]]]:
    """The repo's pytest config file (in pytest's order) and its `testpaths`, `python_files`
    and `pythonpath` options, as lists of strings."""
    keys = ("testpaths", "python_files", "pythonpath")
    if (repo / "pytest.ini").is_file():
        return "pytest.ini", _ini_options(repo / "pytest.ini", "pytest", keys)
    pyproject = _read(repo / "pyproject.toml")
    if pyproject is not None and _PYTEST_CONFIG_SECTIONS["pyproject.toml"].search(pyproject):
        try:
            table = tomllib.loads(pyproject).get("tool", {}).get("pytest", {})
        except tomllib.TOMLDecodeError:
            table = {}
        options = table.get("ini_options", {}) if isinstance(table, dict) else {}
        found = {}
        for key in keys:
            value = options.get(key) if isinstance(options, dict) else None
            if isinstance(value, str):
                found[key] = value.split()
            elif isinstance(value, list):
                found[key] = [str(v) for v in value]
        return "pyproject.toml", found
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        text = _read(repo / name)
        if text is not None and _PYTEST_CONFIG_SECTIONS[name].search(text):
            return name, _ini_options(repo / name, section, keys)
    return None, {}


def _ini_options(path: Path, section: str, keys: tuple[str, ...]) -> dict[str, list[str]]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(_read(path) or "")
    except configparser.Error:
        return {}
    if not parser.has_section(section):
        return {}
    return {k: parser.get(section, k).split() for k in keys if parser.has_option(section, k)}


def _top_level_imports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _has_module(repo: Path, directory: PurePosixPath, name: str) -> bool:
    base = repo / directory
    return (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file()


def _pytest_basedir(repo: Path, directory: PurePosixPath) -> PurePosixPath:
    """The dir pytest's default import mode puts on sys.path for a test file in `directory`:
    the first one upwards that is not a package."""
    while directory != PurePosixPath(".") and (repo / directory / "__init__.py").is_file():
        directory = directory.parent
    return directory


def guess_import_paths(
    repo: Path, files: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Dirs to put on PYTHONPATH so test files can import sibling modules by bare name, and a
    note for each. For every absolute import in a test file whose first name is not a module
    at the repo root or under src/, the nearest dir from the file's own dir upwards that has
    it, unless that is the dir pytest puts on sys.path anyway."""
    paths: dict[str, str] = {}
    root = PurePosixPath(".")
    for rel, source in files:
        directory = PurePosixPath(rel).parent
        if directory == root:
            continue
        base = _pytest_basedir(repo, directory)
        for name in sorted(_top_level_imports(source)):
            if _has_module(repo, root, name) or _has_module(repo, PurePosixPath("src"), name):
                continue
            for candidate in (directory, *directory.parents):
                if candidate == root:
                    break
                if _has_module(repo, candidate, name):
                    key = candidate.as_posix()
                    if candidate != base and key not in paths:
                        paths[key] = (
                            f"added {key}/ to the import path: {rel} imports {name} from it"
                        )
                    break
    return tuple(paths), tuple(paths.values())


def detect_tests(repo: Path) -> TestPlan:
    """Every test is run with pytest (it runs unittest TestCases too and reports JUnit XML),
    given an explicit list of test files so that names pytest would not collect
    (`testapp.py`) still run. A repo whose pytest config sets `testpaths` or `python_files`
    keeps its own collection; its `pythonpath` replaces the import-path guess."""
    rel_files = [f.relative_to(repo).as_posix() for f in iter_files(repo)]
    runnable: list[tuple[str, str]] = []
    for rel in rel_files:
        p = PurePosixPath(rel)
        if p.suffix != ".py" or p.name == "conftest.py":
            continue
        if TEST_FILE_RE.match(p.name):
            runnable.append((rel, _read_source(repo / rel) or ""))
        elif (source := _read_source(repo / rel)) is not None and defines_tests(source):
            runnable.append((rel, source))
    if not runnable:
        return TestPlan(None, (), (), "no test files found")
    test_files = tuple(rel for rel, _ in runnable)

    signal = _pytest_signal(repo, rel_files)
    if signal is None and any(_PYTEST_IMPORT.search(s) for _, s in runnable):
        signal = "test files import pytest"
    reason = signal or "default: pytest also runs unittest tests"
    config, options = pytest_config(repo)
    if options.get("pythonpath"):
        # repo-relative dirs only: the config is untrusted
        import_paths = tuple(
            p.rstrip("/")
            for p in options["pythonpath"]
            if not PurePosixPath(p).is_absolute() and ".." not in PurePosixPath(p).parts
        )
        notes: tuple[str, ...] = (
            f"import path from {config} pythonpath: {', '.join(import_paths)}",
        )
    else:
        import_paths, notes = guess_import_paths(repo, runnable)
    if options.get("testpaths") or options.get("python_files"):
        reason += f"; collection as configured in {config}"
        command = PYTEST_COMMAND
    else:
        command = (*PYTEST_COMMAND, *test_files)
    return TestPlan("pytest", test_files, command, reason, import_paths, notes)


# --- dependencies ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallPlan:
    requirements: tuple[str, ...]
    constraints: tuple[str, ...]  # exact pins from a lockfile, passed to pip with -c
    sources: tuple[str, ...]
    lockfile: str | None
    notes: tuple[str, ...] = field(default=())
    # normalised name -> where it is declared (files read, or the --sandbox-requirements file)
    declared: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    not_installed: tuple[NotInstalled, ...] = ()
    override: str | None = None  # SandboxRequirements.label when a user's file replaced detection
    replaced: tuple[str, ...] = ()  # its lines the fix's declarations replaced ("name: a -> b")


@dataclass(frozen=True)
class SandboxRequirements:
    """A user's requirements file that replaces dependency detection for the sandbox install
    (`--sandbox-requirements`), for the original code and the fix alike."""

    path: str
    sha256: str
    lines: tuple[str, ...]  # requirement and index-option lines, comments dropped

    @property
    def label(self) -> str:
        return f"--sandbox-requirements {PurePosixPath(self.path).name}"

    @property
    def count(self) -> int:
        return sum(not line.startswith("-") for line in self.lines)


def load_sandbox_requirements(path: str | Path) -> SandboxRequirements:
    """Read and check a --sandbox-requirements file; ValueError says what is wrong."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read --sandbox-requirements {path}: {exc}") from None
    problem = sandbox_requirements_problem(text)
    if problem:
        raise ValueError(f"--sandbox-requirements {path}: {problem}")
    return SandboxRequirements(
        str(p.resolve()),
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        tuple(sandbox_requirement_lines(text)),
    )


_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;@]*)")
_PINNED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?\s*==\s*[^\s*;,]+\s*(;.*)?$")
_INCLUDE_RE = re.compile(r"^\s*(-r|-c|--requirement|--constraint)\b", re.M)
_MANIFESTS = ("pyproject.toml", "pixi.toml", "setup.py", "setup.cfg", "Pipfile")


def _req_name(req: str) -> str | None:
    if "://" in req.split("@", 1)[0]:  # bare VCS/URL requirement
        m = re.search(r"#egg=([A-Za-z0-9._-]+)", req)
        return normalize_name(m.group(1)) if m else None
    m = _REQ_NAME_RE.match(req)
    return normalize_name(m.group(1)) if m else None


def exact_pins(plan: InstallPlan) -> dict[str, str]:
    """Normalised name -> the `name==version` line, for every exact pin the install uses
    (requirements and lockfile constraints; a requirement wins over a constraint)."""
    pins: dict[str, str] = {}
    for req in (*plan.requirements, *plan.constraints):
        name = _req_name(req)
        if name and name not in pins and _PINNED_RE.match(req.strip()):
            pins[name] = req.split(";", 1)[0].strip()
    return pins


def _req_clauses(req: str) -> tuple[tuple[str, str], ...]:
    m = _REQ_NAME_RE.match(req)
    return pep440_clauses(m.group(3)) if m else ()


def _project_name(repo: Path) -> str | None:
    text = _read(repo / "pyproject.toml")
    if text:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = {}
        name = data.get("project", {}).get("name") or data.get("tool", {}).get("poetry", {}).get(
            "name"
        )
        if isinstance(name, str):
            return normalize_name(name)
    m = re.search(r"^name\s*=\s*(\S+)", _read(repo / "setup.cfg") or "", re.M)
    if m:
        return normalize_name(m.group(1))
    m = re.search(r"\bname\s*=\s*[\"']([^\"']+)[\"']", _read(repo / "setup.py") or "")
    return normalize_name(m.group(1)) if m else None


def _requirement_files(repo: Path) -> list[str]:
    files = []
    for path in sorted(repo.glob("*")) + sorted((repo / "requirements").glob("*")):
        rel = path.relative_to(repo).as_posix()
        is_req_file = path.suffix in {".txt", ".in"} and is_dependency_file(rel)
        if path.is_file() and not path.is_symlink() and is_req_file:
            files.append(rel)
    return files


def _lock_pins(repo: Path, requirement_files: list[str]) -> tuple[str | None, list[str], list[str]]:
    """(lockfile, pins as 'name==version[; marker]', notes)."""
    notes: list[str] = []
    text = _read(repo / "poetry.lock")
    if text is not None:
        try:
            packages = tomllib.loads(text).get("package", [])
        except tomllib.TOMLDecodeError as exc:
            return None, [], [f"poetry.lock unreadable ({exc}); not used"]
        pins = []
        for pkg in packages:
            source = pkg.get("source", {}).get("type")
            if source not in (None, "legacy"):
                continue  # directory / file / git / url: not an index release
            pin = f"{pkg['name']}=={pkg['version']}"
            if isinstance(pkg.get("markers"), str):
                pin += f"; {pkg['markers']}"
            pins.append(pin)
        return "poetry.lock", pins, notes

    text = _read(repo / "Pipfile.lock")
    if text is not None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, [], [f"Pipfile.lock unreadable ({exc}); not used"]
        pins = []
        for section in ("default", "develop"):
            for name, entry in data.get(section, {}).items():
                if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
                    continue  # path / git / editable entries have no version
                pin = f"{name}{entry['version']}"
                if isinstance(entry.get("markers"), str):
                    pin += f"; {entry['markers']}"
                pins.append(pin)
        return "Pipfile.lock", pins, notes

    pinned_files, pins = [], []
    for rel in requirement_files:
        text = _read(repo / rel) or ""
        if _INCLUDE_RE.search(text):
            continue  # includes other files, which may not be pinned
        reqs = requirements_from_file(rel, text)
        if reqs and all(_PINNED_RE.match(r) for r in reqs):
            pinned_files.append(rel)
            pins += [re.sub(r"\[[^\]]*\]", "", r) for r in reqs]  # constraints can't have extras
    if pinned_files:
        return f"{', '.join(pinned_files)} (fully pinned)", pins, notes
    return None, [], notes


def _filter_pins(
    pins: list[str], requirements: list[str], notes: list[str], lockfile: str
) -> list[str]:
    """Drop lock pins that contradict a declared requirement (e.g. after a fix bumped it),
    and names pinned to several versions without markers to tell them apart."""
    declared: dict[str, list[tuple[tuple[str, str], ...]]] = {}
    for req in requirements:
        if name := _req_name(req):
            declared.setdefault(name, []).append(_req_clauses(req))
    by_name: dict[str, list[str]] = {}
    for pin in pins:
        if name := _req_name(pin):
            by_name.setdefault(name, []).append(pin)
    kept = []
    for name, group in by_name.items():
        if len({p.split(";")[0] for p in group}) > 1 and not all(";" in p for p in group):
            notes.append(f"{lockfile}: {name} pinned to several versions; pin not applied")
            continue
        for pin in group:
            version = pin.split("==", 1)[1].split(";")[0].strip()
            if any(not allows(c, version) for c in declared.get(name, [])):
                notes.append(
                    f"{lockfile}: {name}=={version} conflicts with the declared requirement; "
                    "pin not applied"
                )
                continue
            kept.append(pin)
    return kept


def _build_requirements(repo: Path) -> list[str]:
    """What the build backend needs to install the repo itself (offline, in the test phase)."""
    try:
        return build_system_requires(
            _read(repo / "pyproject.toml") if (repo / "pyproject.toml").is_file() else None,
            has_setup=(repo / "setup.py").is_file() or (repo / "setup.cfg").is_file(),
        )
    except ValueError:
        return ["setuptools", "wheel"]


def _with_tooling(
    repo: Path, runner: Runner | None, reqs: list[str], notes: list[str]
) -> list[str]:
    """`reqs` plus the build backend's requirements and the test runner, without
    self-references to the repo's own project (it is installed from source)."""
    reqs = reqs + _build_requirements(repo)
    own = _project_name(repo)
    if own and any(_req_name(r) == own for r in reqs):
        notes.append(f"self-references to {own} dropped (the repo is installed from source)")
        reqs = [r for r in reqs if _req_name(r) != own]
    if runner == "pytest" and not any(_req_name(r) == "pytest" for r in reqs):
        reqs.append("pytest")
    return reqs


def build_install_plan(
    repo: Path,
    runner: Runner | None,
    override: SandboxRequirements | None = None,
    original: Path | None = None,
) -> InstallPlan:
    """What to install for `repo`. With `override` (a user's requirements file), that file
    replaces the repo's declared dependencies; when `repo` is a fix of `original`, every name
    whose declaration the fix changed takes the fix's declaration instead."""
    if override is not None:
        return _override_plan(repo, runner, override, original)
    notes: list[str] = []
    sources: list[str] = []
    reqs: list[str] = []
    declared: dict[str, list[str]] = {}
    left_out: list[NotInstalled] = []
    req_files = _requirement_files(repo)
    # environment.yml comes last: its pip list yields to every other source
    env_files = [n for n in CONDA_ENVIRONMENT_FILES if (repo / n).is_file()]
    pyproject = repo / "pyproject.toml"
    pixi = has_pixi_manifest(
        _read(pyproject) if pyproject.is_file() else None, (repo / "pixi.toml").is_file()
    )
    for rel in [m for m in _MANIFESTS if (repo / m).is_file()] + req_files + env_files:
        text = _read(repo / rel)
        if text is None:
            notes.append(f"{rel}: unreadable; skipped")
            continue
        try:
            found, skipped = install_requirements(rel, text)
        except (SyntaxError, ValueError, configparser.Error) as exc:
            notes.append(f"{rel}: could not parse ({type(exc).__name__}); skipped")
            continue
        if rel in env_files and pixi:
            # pixi never reads environment.yml: next to a pixi manifest it is another,
            # usually older, environment
            notes.append(
                f"{rel}: not installed (the repo has a pixi manifest, which does not read it)"
            )
            why = "the pixi manifest takes precedence; pixi does not read this file"
            left_out += [
                NotInstalled(name, source, why) for req, source in found if (name := _req_name(req))
            ]
            left_out += [NotInstalled(n.name, n.source, why) for n in skipped]
            continue
        sources.append(rel)
        for req, source in found:
            name = _req_name(req)
            if rel in env_files and name in declared:
                notes.append(
                    f"{source}: {req} not installed ({', '.join(declared[name])} declares {name})"
                )
                continue
            if name and ";" not in req and (why := platform_only(name)):
                left_out.append(NotInstalled(name, source, why))
                continue
            reqs.append(req)
            if source not in sources:
                sources.append(source)
            if name and source not in declared.setdefault(name, []):
                declared[name].append(source)
        left_out += skipped

    reqs = _with_tooling(repo, runner, reqs, notes)
    lockfile, pins, lock_notes = _lock_pins(repo, req_files)
    notes += lock_notes
    if lockfile:
        pins = _filter_pins(pins, reqs, notes, lockfile)
    return InstallPlan(
        requirements=tuple(dict.fromkeys(reqs)),
        constraints=tuple(dict.fromkeys(pins)),
        sources=tuple(sources),
        lockfile=lockfile,
        notes=tuple(notes),
        declared={name: tuple(where) for name, where in declared.items()},
        # a package installed anyway (another source, or apifix's own tooling) is not missing
        not_installed=tuple(
            n for n in left_out if n.name not in declared and n.name not in _by_name(reqs)
        ),
    )


def _by_name(reqs: Iterable[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for req in reqs:
        if name := _req_name(req):
            out.setdefault(name, []).append(req)
    return out


def _override_plan(
    repo: Path, runner: Runner | None, override: SandboxRequirements, original: Path | None
) -> InstallPlan:
    notes = [
        f"dependencies from {override.label} ({override.count} requirement(s)), replacing "
        "apifix's detection"
    ]
    reqs = list(override.lines)
    declared = {name: (override.label,) for line in reqs if (name := _req_name(line))}
    replaced = []
    if original is not None:
        # The fix's own dependency edits still count: compare what detection finds in both
        # trees, and let the fix's declaration win for every name it changed.
        fixed_plan = build_install_plan(repo, None)
        original_plan = build_install_plan(original, None)
        new, old = _by_name(fixed_plan.requirements), _by_name(original_plan.requirements)
        for name in sorted(set(new) | set(old)):
            if new.get(name) == old.get(name):
                continue
            before = [r for r in reqs if _req_name(r) == name]
            reqs = [r for r in reqs if _req_name(r) != name] + new.get(name, [])
            if name in new:
                declared[name] = fixed_plan.declared.get(name, ())
            else:
                declared.pop(name, None)
            replaced.append(
                f"{name}: {', '.join(before) or 'not listed'} -> "
                f"{', '.join(new.get(name, [])) or 'removed by the fix'}"
            )
    reqs = _with_tooling(repo, runner, reqs, notes)
    return InstallPlan(
        requirements=tuple(dict.fromkeys(reqs)),
        constraints=(),
        sources=(override.label,),
        lockfile=None,
        notes=tuple(notes),
        declared=declared,
        override=override.label,
        replaced=tuple(replaced),
    )
