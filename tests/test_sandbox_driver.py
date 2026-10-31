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
    assert driver.main(["test", "--path"]) == 2
    assert driver.main(["test", "--bogus", "--", "x"]) == 2


SIBLING = ["{python}", "-c", "import server; assert server.VALUE == 7"]


@pytest.mark.parametrize("pyproject", [None, PYPROJECT_OK])
def test_import_paths_go_on_pythonpath(env, pyproject):
    work, results = env
    if pyproject:
        (work / "pyproject.toml").write_text(pyproject)
        (work / "backend.py").write_text(textwrap.dedent(BACKEND))
    (work / "svc").mkdir()
    (work / "svc" / "__init__.py").write_text("")
    (work / "svc" / "server.py").write_text("VALUE = 7\n")
    assert driver.test(SIBLING) != 0  # not importable by bare name without the path
    assert driver.test(SIBLING, paths=["svc"]) == 0
    report = _report(results)
    assert report["import_paths"] == ["svc"]
    assert report["install_method"] == ("editable" if pyproject else "pythonpath")


def test_main_passes_paths(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        driver, "test", lambda cmd, coverage=False, paths=(): seen.update(p=paths, c=cmd) or 0
    )
    assert driver.main(["test", "--path", "a", "--coverage", "--path", "b/c", "--", "x"]) == 0
    assert seen == {"p": ["a", "b/c"], "c": ["x"]}


def _smoke(tmp_path, monkeypatch, capsys, code: str, distribution: str = "pip") -> dict:
    check = tmp_path / "smoke_check.py"
    check.write_text(code)
    monkeypatch.setattr(driver, "SMOKE_CHECK", str(check))
    assert driver.main(["smoke", distribution]) == 0
    (line,) = [
        ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(driver.SMOKE_MARKER)
    ]
    return json.loads(line[len(driver.SMOKE_MARKER) :])


def test_smoke_check_passes(env, tmp_path, monkeypatch, capsys):
    result = _smoke(tmp_path, monkeypatch, capsys, "import pip\n")
    assert result["ok"] is True
    assert result["installed"]["distribution"] == "pip"
    assert result["installed"]["version"]  # read from the deps venv's metadata


def test_smoke_check_failure_keeps_the_traceback(env, tmp_path, monkeypatch, capsys):
    code = "raise TypeError(\"Client.__init__() got an unexpected keyword argument 'proxies'\")\n"
    result = _smoke(tmp_path, monkeypatch, capsys, code)
    assert result["ok"] is False
    assert "unexpected keyword argument 'proxies'" in result["output"]


def test_smoke_check_missing_distribution(env, tmp_path, monkeypatch, capsys):
    result = _smoke(tmp_path, monkeypatch, capsys, "import nothing_here\n", "not-installed-dist")
    assert result["ok"] is False
    assert result["installed"] == {
        "distribution": "not-installed-dist",
        "version": None,
        "requires": {},
    }


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
    monkeypatch.setattr(
        driver, "test", lambda cmd, coverage=False, paths=(): seen.update(c=coverage) or 0
    )
    assert driver.main(["test", "--coverage", "--", "x"]) == 0
    assert seen == {"c": True}


# --- install: download figures --------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "key", "size"),
    [
        ("  Downloading lxml-4.9.2-cp311-cp311-manylinux.whl (7.2 MB)", "downloaded", 7_200_000),
        ("  Using cached six-1.16.0-py2.py3-none-any.whl (11 kB)", "cached", 11_000),
        ("Downloading https://files.example/x-1.0.tar.gz (1.5MB)", "downloaded", 1_500_000),
        ("  Downloading tiny-1.0-py3-none-any.whl (912 bytes)", "downloaded", 912),
        ("  Downloading python_dotenv-1.0.0-py3-none-any.whl.metadata (21 kB)", None, 0),
        ("Collecting requests==2.28.2", None, 0),
    ],
)
def test_count_file_line(line, key, size):
    stats = {"downloaded": 0, "downloaded_bytes": 0, "cached": 0, "cached_bytes": 0}
    driver.count_file_line(line, stats)
    if key is None:
        assert set(stats.values()) == {0}
    else:
        assert (stats[key], stats[key + "_bytes"]) == (1, size)


def test_install_passes_pip_output_through_and_reports_figures(tmp_path, monkeypatch, capsys):
    deps = tmp_path / "deps"
    deps.mkdir()
    monkeypatch.setattr(driver, "DEPS", str(deps))

    def fake_venv(cmd, **kw):  # a "python" that prints what pip would and exits 3
        bin_dir = Path(cmd[-1]) / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").write_text(
            "#!/bin/sh\n"
            "echo 'Collecting six'\n"
            "echo '  Downloading six-1.16.0-py2.py3-none-any.whl.metadata (1.8 kB)'\n"
            "echo '  Downloading six-1.16.0-py2.py3-none-any.whl (11 kB)'\n"
            "echo '  Using cached lxml-4.9.2-cp311-manylinux.whl (7.2 MB)'\n"
            "exit 3\n"
        )
        (bin_dir / "python").chmod(0o755)

    monkeypatch.setattr(driver.subprocess, "check_call", fake_venv)

    seen = {}
    real_popen = driver.subprocess.Popen

    def spying_popen(cmd, **kw):
        seen["cmd"] = cmd
        return real_popen(cmd, **kw)

    monkeypatch.setattr(driver.subprocess, "Popen", spying_popen)
    assert driver.install(False) == 3
    assert "--timeout" in seen["cmd"] and "--retries" in seen["cmd"]
    out = capsys.readouterr().out
    assert "  Downloading six-1.16.0-py2.py3-none-any.whl (11 kB)\n" in out  # unchanged
    marker = out.strip().splitlines()[-1]
    assert marker.startswith(driver.INSTALL_STATS_MARKER)
    stats = json.loads(marker[len(driver.INSTALL_STATS_MARKER) :])
    assert stats == {
        "downloaded": 1,
        "downloaded_bytes": 11_000,
        "cached": 1,
        "cached_bytes": 7_200_000,
    }
