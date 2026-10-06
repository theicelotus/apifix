"""Run the in-container driver on the host, against temp dirs, with real venvs and pip.

No network or Docker needed: this exercises the overlay venv, the .pth bridge to the read-only
deps venv, the offline editable install and its PYTHONPATH fallback.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from apifix.engine import sandbox_driver as driver

# A minimal in-tree PEP 660 backend, so an offline editable install can really succeed.
BACKEND = """
import base64, hashlib, os, zipfile

NAME, VERSION = "demo", "0.1"


def _line(path, data):
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{path},sha256={digest},{len(data)}"


def get_requires_for_build_editable(config_settings=None):
    return []


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    dist = f"{NAME}-{VERSION}.dist-info"
    files = {
        f"{NAME}.pth": (os.path.abspath("src") + "\\n").encode(),
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.1\\nName: {NAME}\\nVersion: {VERSION}\\n".encode()
        ),
        f"{dist}/WHEEL": (
            b"Wheel-Version: 1.0\\nGenerator: t\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"
        ),
    }
    wheel = f"{NAME}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, wheel), "w") as z:
        record = []
        for path, data in files.items():
            z.writestr(path, data)
            record.append(_line(path, data))
        record.append(f"{dist}/RECORD,,")
        z.writestr(f"{dist}/RECORD", "\\n".join(record) + "\\n")
    return wheel
"""

PYPROJECT_OK = """
[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]

[project]
name = "demo"
version = "0.1"
"""

PYPROJECT_BROKEN = """
[build-system]
requires = ["backend-that-is-not-installed"]
build-backend = "not_installed_backend"
"""


@pytest.fixture(scope="module")
def deps_dir(tmp_path_factory) -> Path:
    """A deps dir with a venv that has pip (from ensurepip, offline)."""
    deps = tmp_path_factory.mktemp("deps")
    subprocess.check_call([sys.executable, "-m", "venv", str(deps / "venv")])
    return deps


@pytest.fixture
def env(tmp_path, deps_dir, monkeypatch):
    work = tmp_path / "work"
    (work / "src" / "demo").mkdir(parents=True)
    (work / "src" / "demo" / "__init__.py").write_text("VALUE = 42\n")
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(driver, "DEPS", str(deps_dir))
    monkeypatch.setattr(driver, "WORK", str(work))
    monkeypatch.setattr(driver, "RESULTS", str(results))
    monkeypatch.setattr(driver, "OVERLAY", str(tmp_path / "overlay"))
    return work, results


def _report(results: Path) -> dict:
    return json.loads((results / "driver.json").read_text())


CHECK = [
    "{python}",
    "-c",
    "import demo, pip, sys; assert demo.VALUE == 42; print(sys.prefix)",
]


def test_editable_install_offline(env, tmp_path):
    work, results = env
    (work / "pyproject.toml").write_text(PYPROJECT_OK)
    (work / "backend.py").write_text(textwrap.dedent(BACKEND))
    assert driver.test(CHECK) == 0
    report = _report(results)
    assert report["install_method"] == "editable", report["install_log"]
    assert report["exit_code"] == 0
    assert report["command"][0] == str(tmp_path / "overlay" / "bin" / "python")


def test_falls_back_to_pythonpath_when_editable_install_fails(env):
    work, results = env
    (work / "pyproject.toml").write_text(PYPROJECT_BROKEN)
    assert driver.test(CHECK) == 0  # demo still importable, via PYTHONPATH
    report = _report(results)
    assert report["install_method"] == "pythonpath"
    assert "No module named 'not_installed_backend'" in report["install_log"]


def test_pythonpath_when_not_installable(env):
    _, results = env
    assert driver.test(CHECK) == 0
    report = _report(results)
    assert report["install_method"] == "pythonpath"
    assert report["install_log"] == "no pyproject.toml or setup.py"


def test_exit_code_is_propagated_and_recorded(env):
    _, results = env
    assert driver.test(["{python}", "-c", "raise SystemExit(3)"]) == 3
    assert _report(results)["exit_code"] == 3


def test_usage_error():
    assert driver.main(["bogus"]) == 2


@pytest.fixture(scope="module")
def deps_with_coverage(tmp_path_factory) -> Path:
    """Like deps_dir, but `coverage` is importable (borrowed from apifix's own dev venv)."""
    import coverage

    deps = tmp_path_factory.mktemp("deps-cov")
    subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", str(deps / "venv")])
    site = driver._site_dirs(str(deps / "venv" / "bin" / "python"))[0]
    Path(site, "borrowed.pth").write_text(str(Path(coverage.__file__).parent.parent) + "\n")
    return deps


def test_coverage_wraps_the_test_command(env, deps_with_coverage, monkeypatch):
    work, results = env
    monkeypatch.setattr(driver, "DEPS", str(deps_with_coverage))
    (work / "src" / "demo" / "__init__.py").write_text(
        "def used():\n    return 1\n\n\ndef unused():\n    return 2\n"
    )
    (work / "test_demo.py").write_text("import demo\n\nassert demo.used() == 1\n")
    rc = driver.test(["{python}", "-m", "test_demo"], coverage=True)
    assert rc == 0
    report = _report(results)
    assert report["command"][2:4] == ["coverage", "run"]
    assert report["coverage_exit_code"] == 0
    data = json.loads((results / "coverage.json").read_text())
    (key,) = [k for k in data["files"] if k.endswith("demo/__init__.py")]
    assert data["files"][key]["executed_lines"] == [1, 2, 5]  # `unused` body not run
    assert data["files"][key]["missing_lines"] == [6]


def test_coverage_flag_in_main(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(driver, "test", lambda cmd, coverage=False: seen.update(c=coverage) or 0)
    assert driver.main(["test", "--coverage", "--", "x"]) == 0
    assert seen == {"c": True}
