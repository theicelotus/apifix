from __future__ import annotations

import pytest

from apifix.engine.scope import tooling_change

PYPROJECT = """
[project]
name = "x"
dependencies = ["openai==0.28.1"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
addopts = "-q"
"""


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", "tox.ini", "noxfile.py", "Makefile", ".gitlab-ci.yml"]
)
def test_ci_files(path):
    assert tooling_change(path, "a = 1\n", "a = 2\n")


def test_dependency_pin_change_is_not_tooling():
    new = PYPROJECT.replace("openai==0.28.1", "openai>=1.0")
    assert tooling_change("pyproject.toml", PYPROJECT, new) is None


def test_pytest_section_is_left_to_tampering_checks():
    new = PYPROJECT.replace('addopts = "-q"', 'addopts = "-q -k chat"')
    assert tooling_change("pyproject.toml", PYPROJECT, new) is None


def test_other_pyproject_settings_are_tooling():
    new = PYPROJECT.replace("line-length = 100", "line-length = 120")
    assert tooling_change("pyproject.toml", PYPROJECT, new) == (
        "settings other than dependency pins changed: tool.ruff.line-length"
    )


def test_setup_cfg():
    old = "[metadata]\nname = x\n[options]\ninstall_requires =\n    openai==0.28.1\n"
    assert tooling_change("setup.cfg", old, old.replace("0.28.1", "1.3")) is None
    assert "metadata.name" in tooling_change("setup.cfg", old, old.replace("name = x", "name = y"))


def test_tox_ini_sections_other_than_pytest():
    old = "[tox]\nenvlist = py311\n[pytest]\naddopts = -q\n"
    assert tooling_change("tox.ini", old, old.replace("-q", "-q -x")) is None
    assert "tox.envlist" in tooling_change("tox.ini", old, old.replace("py311", "py312"))


def test_nested_config_files_and_source_are_not_tooling():
    assert tooling_change("sub/pyproject.toml", PYPROJECT, PYPROJECT + "\n# x\n") is None
    assert tooling_change("app.py", "a", "b") is None


def test_unparseable_config():
    assert "could not be parsed" in tooling_change("pyproject.toml", PYPROJECT, "[x\n")


PIXI = """\
[tool.pixi.workspace]
channels = ["conda-forge"]

[tool.pixi.pypi-dependencies]
openai = "==0.28"

[tool.pixi.target.linux-64.pypi-dependencies]
httpx = "<0.28"

[tool.pixi.feature.test.pypi-dependencies]
pytest = ">=7"

[tool.pixi.feature.test.target.unix.pypi-dependencies]
openai = "==0.28"
"""


def test_pixi_pypi_dependency_edits_are_not_tooling_changes():
    new = PIXI.replace('"==0.28"', '">=1.0,<2"').replace('"<0.28"', '">=0.27"')
    assert tooling_change("pyproject.toml", PIXI, new) is None


def test_other_pixi_settings_are():
    new = PIXI.replace('"conda-forge"', '"defaults"')
    assert tooling_change("pyproject.toml", PIXI, new) == (
        "settings other than dependency pins changed: tool.pixi.workspace.channels"
    )
