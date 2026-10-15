"""Facts about a target repo that the sandbox needs: Python version, test runner, dependencies.

Everything here reads files only; nothing from the repo is imported or executed.
"""

from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from apifix.engine.dependencies import (
    allows,
    build_system_requires,
    is_dependency_file,
    normalize_name,
    pep440_clauses,
    poetry_clauses,
    requirements_from_file,
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
    test_files: tuple[str, ...]
    command: tuple[str, ...]  # "{python}" is replaced by the interpreter inside the sandbox
    reason: str


TEST_FILE_RE = re.compile(r"^(test_.*|.*_test|tests)\.py$")
_PYTEST_CONFIG_SECTIONS = {
    "pyproject.toml": re.compile(r"^\[tool\.pytest(\.ini_options)?\]", re.M),
    "setup.cfg": re.compile(r"^\[tool:pytest\]", re.M),
    "tox.ini": re.compile(r"^\[pytest\]", re.M),
}
_UNITTEST_IMPORT = re.compile(r"^\s*(import unittest|from unittest\b)", re.M)
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


def detect_tests(repo: Path) -> TestPlan:
    rel_files = [f.relative_to(repo).as_posix() for f in iter_files(repo)]
    test_files = tuple(f for f in rel_files if TEST_FILE_RE.match(Path(f).name))
    if not test_files:
        return TestPlan(None, (), (), "no test files found")

    signal = _pytest_signal(repo, rel_files)
    sources = [_read(repo / f) or "" for f in test_files]
    if signal is None and any(_PYTEST_IMPORT.search(s) for s in sources):
        signal = "test files import pytest"
    if signal is None and all(_UNITTEST_IMPORT.search(s) for s in sources):
        command = ["{python}", "-m", "unittest", "discover", "-v"]
        # Discovery only recurses into packages; point it at a plain tests/ dir directly.
        for d in ("tests", "test"):
            if (repo / d).is_dir() and not (repo / d / "__init__.py").exists():
                command[4:4] = ["-s", d]
                break
        return TestPlan("unittest", test_files, tuple(command), "all test files use unittest")
    # pytest also runs unittest-style tests, and reports per-test results as JUnit XML.
    return TestPlan("pytest", test_files, PYTEST_COMMAND, signal or "default for plain tests")


# --- dependencies ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallPlan:
    requirements: tuple[str, ...]
    constraints: tuple[str, ...]  # exact pins from a lockfile, passed to pip with -c
    sources: tuple[str, ...]
    lockfile: str | None
    notes: tuple[str, ...] = field(default=())


_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*([^;@]*)")
_PINNED_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?\s*==\s*[^\s*;,]+\s*(;.*)?$")
_INCLUDE_RE = re.compile(r"^\s*(-r|-c|--requirement|--constraint)\b", re.M)
_MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")


def _req_name(req: str) -> str | None:
    if "://" in req.split("@", 1)[0]:  # bare VCS/URL requirement
        m = re.search(r"#egg=([A-Za-z0-9._-]+)", req)
        return normalize_name(m.group(1)) if m else None
    m = _REQ_NAME_RE.match(req)
    return normalize_name(m.group(1)) if m else None


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


def build_install_plan(repo: Path, runner: Runner | None) -> InstallPlan:
    notes: list[str] = []
    sources: list[str] = []
    reqs: list[str] = []
    req_files = _requirement_files(repo)
    for rel in [m for m in _MANIFESTS if (repo / m).is_file()] + req_files:
        text = _read(repo / rel)
        if text is None:
            notes.append(f"{rel}: unreadable; skipped")
            continue
        try:
            reqs += requirements_from_file(rel, text)
        except (SyntaxError, ValueError, configparser.Error) as exc:
            notes.append(f"{rel}: could not parse ({type(exc).__name__}); skipped")
            continue
        sources.append(rel)

    try:
        reqs += build_system_requires(
            _read(repo / "pyproject.toml") if (repo / "pyproject.toml").is_file() else None,
            has_setup=(repo / "setup.py").is_file() or (repo / "setup.cfg").is_file(),
        )
    except ValueError:
        reqs += ["setuptools", "wheel"]

    own = _project_name(repo)
    if own and any(_req_name(r) == own for r in reqs):
        notes.append(f"self-references to {own} dropped (the repo is installed from source)")
        reqs = [r for r in reqs if _req_name(r) != own]
    if runner == "pytest" and not any(_req_name(r) == "pytest" for r in reqs):
        reqs.append("pytest")

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
    )
