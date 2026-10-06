"""Changes a migration fix should not need: CI and tooling configuration."""

from __future__ import annotations

import configparser
import tomllib
from pathlib import PurePosixPath

CI_DIRS = (".github/", ".circleci/", ".gitlab/", ".buildkite/")
CI_FILES = {
    "tox.ini",
    "noxfile.py",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "Jenkinsfile",
    ".gitlab-ci.yml",
    ".travis.yml",
    "azure-pipelines.yml",
    ".pre-commit-config.yaml",
}

# Dependency declarations a fix may legitimately change. pytest settings are excluded too:
# they are checked (and rejected) by the tampering checks instead.
_PYPROJECT_DEPENDENCY_KEYS = (
    ("project", "dependencies"),
    ("project", "optional-dependencies"),
    ("dependency-groups",),
    ("tool", "poetry", "dependencies"),
    ("tool", "poetry", "dev-dependencies"),
    ("tool", "poetry", "group"),
    ("tool", "pytest"),
)
_SETUP_CFG_DEPENDENCY_OPTIONS = {
    ("options", "install_requires"),
    ("options", "setup_requires"),
    ("options", "tests_require"),
}
_INI_SKIPPED_SECTIONS = {"options.extras_require", "tool:pytest", "pytest"}


def _drop(data: dict, path: tuple[str, ...]) -> None:
    *parents, last = path
    for key in parents:
        data = data.get(key)
        if not isinstance(data, dict):
            return
    data.pop(last, None)


def _flatten(data: object, prefix: str = "") -> dict[str, object]:
    if not isinstance(data, dict):
        return {prefix: data}
    out: dict[str, object] = {}
    for key, value in data.items():
        out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    return out


def _pyproject_settings(text: str) -> dict[str, object]:
    data = tomllib.loads(text)
    for path in _PYPROJECT_DEPENDENCY_KEYS:
        _drop(data, path)
    return _flatten(data)


def _ini_settings(text: str) -> dict[str, object]:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    out: dict[str, object] = {}
    for section in cfg.sections():
        if section in _INI_SKIPPED_SECTIONS:
            continue
        for option, value in cfg.items(section, raw=True):
            if (section, option) not in _SETUP_CFG_DEPENDENCY_OPTIONS:
                out[f"{section}.{option}"] = value
    return out


def tooling_change(path: str, old_text: str | None, new_text: str | None) -> str | None:
    """A description if this change touches CI/tooling configuration, else None."""
    name = PurePosixPath(path).name
    if path.startswith(CI_DIRS) or (name in CI_FILES and name != "tox.ini"):
        return "CI/tooling file changed"
    parsers = {
        "pyproject.toml": _pyproject_settings,
        "setup.cfg": _ini_settings,
        "tox.ini": _ini_settings,
    }
    parser = parsers.get(name) if path == name else None  # repo-root config files only
    if parser is None:
        return None
    try:
        old = parser(old_text) if old_text is not None else {}
        new = parser(new_text) if new_text is not None else {}
    except (ValueError, configparser.Error):
        return f"{name} changed and could not be parsed"
    keys = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    if not keys:
        return None
    shown = ", ".join(keys[:5]) + (f" and {len(keys) - 5} more" if len(keys) > 5 else "")
    return f"settings other than dependency pins changed: {shown}"
