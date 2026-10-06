from __future__ import annotations

import textwrap

import pytest

from apifix.engine.dependencies import (
    allows,
    build_system_requires,
    entirely_below,
    is_dependency_file,
    parse_dependency_file,
    parse_version,
    requirements_from_file,
)
from apifix.engine.scanner import scan


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("requirements.txt", True),
        ("requirements-dev.txt", True),
        ("dev-requirements.in", True),
        ("requirements/base.txt", True),
        ("constraints.txt", True),
        ("pyproject.toml", True),
        ("sub/setup.py", True),
        ("setup.cfg", True),
        ("Pipfile", True),
        ("notes.txt", False),
        ("main.py", False),
    ],
)
def test_is_dependency_file(path, expected):
    assert is_dependency_file(path) is expected


def test_parse_version():
    assert parse_version("0.28.1") == (0, 28, 1)
    assert parse_version("1.0.0") == (1,)
    assert parse_version("1.3.0rc1") == (1, 3)
    assert parse_version("abc") is None


@pytest.mark.parametrize(
    ("clauses", "below"),
    [
        ((("==", "0.28.1"),), True),
        ((("==", "0.28.*"),), True),
        ((("<", "1"),), True),
        ((("<", "1.0"),), True),
        ((("<", "0.29"),), True),
        ((("<=", "0.28.1"),), True),
        ((("~=", "0.27"),), True),
        ((("~=", "0.27.1"),), True),
        (((">=", "0.27"), ("<", "1.0")), True),
        ((("<=", "1.0"),), False),
        ((("==", "1.3.0"),), False),
        (((">=", "0.27"),), False),
        ((("~=", "1.0"),), False),
        ((("!=", "0.28"),), False),
        ((), False),
    ],
)
def test_entirely_below(clauses, below):
    assert entirely_below(clauses, "1.0") is below


def _openai(relpath, text):
    deps = parse_dependency_file(relpath, textwrap.dedent(text))
    return [d for d in deps if d.name == "openai"]


def test_requirements_parsing():
    deps = parse_dependency_file(
        "requirements.txt",
        "# pinned\n-r base.txt\nrequests>=2\nOpenAI[datalib]==0.28.1 ; python_version>'3.8'  # x\n"
        "openai-whisper==1.0\nlangchain_openai\n",
    )
    names = [d.name for d in deps]
    assert names == ["requests", "openai", "openai-whisper", "langchain-openai"]
    openai = deps[1]
    assert openai.clauses == (("==", "0.28.1"),)
    assert openai.line == 4


def test_pyproject_pep621_and_poetry():
    deps = _openai(
        "pyproject.toml",
        """
        [project]
        dependencies = ["httpx", "openai<1"]

        [project.optional-dependencies]
        ai = ["openai>=0.27,<0.29"]

        [tool.poetry.dependencies]
        python = "^3.10"
        openai = "^0.28"

        [tool.poetry.group.dev.dependencies]
        openai = { version = "~0.27" }
        """,
    )
    assert [(d.line, entirely_below(d.clauses, "1.0")) for d in deps] == [
        (3, True),
        (6, True),
        (10, True),
        (10, True),  # same key name: located at its first occurrence
    ]


@pytest.mark.parametrize(
    ("spec", "below"),
    [
        ("^0.28", True),
        ("^0.28.1", True),
        ("~0.28", True),
        ("0.28.1", True),
        ("^1.3", False),
        ("*", False),
        (">=0.27,<1.0", True),
        ("^0.28 || ^1.0", False),
    ],
)
def test_poetry_specs(spec, below):
    (dep,) = _openai("pyproject.toml", f'[tool.poetry.dependencies]\nopenai = "{spec}"\n')
    assert entirely_below(dep.clauses, "1.0") is below


def test_setup_py_and_cfg():
    (py,) = _openai(
        "setup.py",
        """
        from setuptools import setup
        setup(
            name="x",
            install_requires=["requests", "openai~=0.27"],
        )
        """,
    )
    assert (py.line, py.col, py.clauses) == (5, 36, (("~=", "0.27"),))
    (cfg,) = _openai(
        "setup.cfg",
        """
        [options]
        install_requires =
            requests
            openai==0.28.0
        """,
    )
    assert cfg.line == 5 and cfg.clauses == (("==", "0.28.0"),)


def test_pipfile():
    deps = _openai(
        "Pipfile",
        """
        [packages]
        openai = "==0.28.1"

        [dev-packages]
        openai = {version = "*"}
        """,
    )
    assert [entirely_below(d.clauses, "1.0") for d in deps] == [True, False]


# --- through the scanner --------------------------------------------------------------------


def test_scanner_reports_dependency_pins(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\nopenai==0.28.1\n")
    (tmp_path / "requirements-ok.txt").write_text("openai>=1.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\ndependencies=["openai<1"]\n')
    (tmp_path / "setup.py").write_text(
        'from setuptools import setup\nsetup(install_requires=["openai~=0.27"])\n'
    )
    (tmp_path / "Pipfile").write_text('[packages]\nopenai = "==0.27.8"\n')
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = x\n")
    result = scan(tmp_path, "openai-v1")
    deps = [f for f in result.findings if f.category == "dependency"]
    assert [(f.file, f.line, f.old_pattern) for f in deps] == [
        ("Pipfile", 2, "openai = '==0.27.8'"),
        ("pyproject.toml", 3, "openai<1"),
        ("requirements.txt", 2, "openai==0.28.1"),
        ("setup.py", 2, "openai~=0.27"),
    ]
    assert all("openai>=1.0" in f.suggestion for f in deps)
    assert result.parse_errors == []
    assert result.files_scanned == 6


def test_unpinned_openai_not_flagged(tmp_path):
    (tmp_path / "requirements.txt").write_text("openai\n")
    assert scan(tmp_path, "openai-v1").findings == []


def test_malformed_dependency_file_recorded(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project\n")
    (tmp_path / "requirements.txt").write_text("openai==0.28\n")
    result = scan(tmp_path, "openai-v1")
    assert [e.file for e in result.parse_errors] == ["pyproject.toml"]
    assert len(result.findings) == 1


@pytest.mark.parametrize(
    ("clauses", "version", "ok"),
    [
        ((), "1.0", True),
        (((">=", "3.8"),), "3.11", True),
        (((">=", "3.8"), ("<", "3.11")), "3.11", False),
        ((("~=", "3.9.0"),), "3.9.18", True),
        ((("~=", "3.9.0"),), "3.10.0", False),
        ((("==", "3.11.*"),), "3.11.4", True),
        ((("!=", "3.11.*"),), "3.11.4", False),
        ((("==", "0.28.1"),), "0.28.1", True),
        ((("<=", "1.0"),), "1.0.0", True),
        (((">", "1.0"),), "1.0", False),
    ],
)
def test_allows(clauses, version, ok):
    assert allows(clauses, version) is ok


def test_requirements_txt_keeps_markers_and_drops_local_code():
    text = "requests>=2 ; python_version >= '3.8'\n-e .\n./vendored\nfile:../x\nfoo @ file:///tmp/foo\n"
    assert requirements_from_file("requirements.txt", text) == [
        "requests>=2 ; python_version >= '3.8'"
    ]


def test_build_system_requires():
    assert build_system_requires('[build-system]\nrequires = ["flit_core>=3.2"]\n', False) == [
        "flit_core>=3.2"
    ]
    assert build_system_requires(None, has_setup=True) == ["setuptools", "wheel"]
    assert build_system_requires(None, has_setup=False) == []
