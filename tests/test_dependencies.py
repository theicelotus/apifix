from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apifix.engine.dependencies import (
    allows,
    build_system_requires,
    dependency_file_problem,
    distribution_candidates,
    entirely_at_or_above,
    entirely_below,
    install_requirements,
    is_dependency_file,
    parse_conda_environment,
    parse_dependency_file,
    parse_version,
    platform_only,
    requirements_from_file,
    sandbox_pixi_targets,
    sandbox_requirement_lines,
    sandbox_requirements_problem,
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


@pytest.mark.parametrize(
    ("clauses", "above"),
    [
        ((("==", "1.12.0"),), True),
        ((("==", "1.*"),), True),
        ((("===", "1.0"),), True),
        (((">=", "1.0"),), True),
        (((">=", "1"),), True),
        (((">=", "1.0"), ("<", "2")), True),
        (((">", "1.0"),), True),
        ((("~=", "1.3"),), True),
        ((("<", "2"), (">=", "1.2")), True),
        ((("==", "0.28.1"),), False),
        ((("==", "0.*"),), False),
        (((">=", "0.27"),), False),
        (((">", "0.99"),), False),  # 0.99.1 is allowed
        ((("~=", "0.27"),), False),
        ((("<", "2"),), False),
        ((("!=", "0.28"),), False),
        ((), False),
    ],
)
def test_entirely_at_or_above(clauses, above):
    assert entirely_at_or_above(clauses, "1.0") is above


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
        (13, True),  # same key name: located inside its own table
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


@pytest.mark.parametrize(
    ("line", "confidence", "says"),
    [
        ("openai", "likely", "the declaration has no version"),
        ("openai[datalib]", "likely", "the declaration has no version"),
        ("openai>=0.27", "likely", "`openai>=0.27` still allows 0.x"),
        ("openai<2", "likely", "`openai<2` still allows 0.x"),
        ("openai<3", "likely", "`openai<3` still allows 0.x"),
        ("openai!=0.28.0", "likely", "still allows 0.x"),
        ("openai==0.28.1", "certain", "Pin openai>=1.0"),
    ],
)
def test_openai_declarations_that_allow_0x_are_flagged(tmp_path, line, confidence, says):
    (tmp_path / "requirements.txt").write_text(f"{line}\n")
    (finding,) = scan(tmp_path, "openai-v1").findings
    assert (finding.category, finding.confidence) == ("dependency", confidence)
    assert says in finding.suggestion and "openai>=1.55.3,<3" in finding.suggestion


@pytest.mark.parametrize(
    "line",
    [
        "openai>=1.0",
        "openai>=1.3,<2",
        "openai==1.12.0",
        "openai~=1.3",
        "openai>=1.55.3,<3",
        "openai==2.54.0",
        "openai>=2",
        "openai @ git+https://x/o",
    ],
)
def test_openai_declarations_on_1x_or_direct_are_not_flagged(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(f"{line}\n")
    assert scan(tmp_path, "openai-v1").findings == []


def test_unpinned_openai_in_tables_flagged_unless_a_direct_reference(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [tool.poetry.dependencies]
            openai = "*"
            [tool.poetry.group.dev.dependencies]
            openai = {git = "https://github.com/openai/openai-python"}
            """
        )
    )
    (tmp_path / "Pipfile").write_text('[packages]\nopenai = {path = "../openai"}\n')
    findings = scan(tmp_path, "openai-v1").findings
    assert [(f.file, f.line, f.confidence) for f in findings] == [("pyproject.toml", 2, "likely")]


def test_direct_references_are_marked():
    deps = parse_dependency_file("requirements.txt", "openai @ https://x/openai-0.28.whl\nopenai\n")
    assert [(d.direct, d.clauses) for d in deps] == [(True, ()), (False, ())]
    deps = parse_dependency_file(
        "pyproject.toml",
        '[tool.pixi.pypi-dependencies]\na = {git = "x"}\nb = {url = "x"}\nc = {version = "*"}\n'
        'd = {extras = ["x"]}\n',
    )
    assert [(d.name, d.direct) for d in deps] == [
        ("a", True),
        ("b", True),
        ("c", False),
        ("d", False),
    ]


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


# --- well-formedness (whole-file mode's check) ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "openai>=1.55.3,<3\npytest\n",
        "# pinned\nopenai==1.3.0  # comment\n\n-r base.txt\n-e .\n",
        "openai[datalib]>=1 ; python_version >= '3.8'\n",
        "pkg @ https://example.com/pkg.whl\n",
        "git+https://github.com/o/r.git\n",
        "openai~=1.3\nnumpy!=1.25.0\n",
        "",
    ],
)
def test_well_formed_requirements(text):
    assert dependency_file_problem("requirements.txt", text) is None


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("Here is the updated file:\nopenai>=1\n", 1),
        ("openai>=1\n```\n", 2),
        ("openai>=>1\n", 1),
        ("openai>=1,\n", 1),
        ("openai 1.0\n", 1),
    ],
)
def test_malformed_requirements(text, line):
    problem = dependency_file_problem("requirements.txt", text)
    assert problem is not None and problem.startswith(f"line {line} is not a valid requirement")


def test_structured_dependency_files_must_parse():
    toml = '[project]\ndependencies = ["openai>=1"]\n'
    assert dependency_file_problem("pyproject.toml", toml) is None
    assert dependency_file_problem("pyproject.toml", "[project\n").startswith("TOMLDecodeError")
    assert dependency_file_problem("setup.py", "setup(\n").startswith("SyntaxError")
    problem = dependency_file_problem("setup.cfg", "install_requires = openai\n")
    assert problem.startswith("MissingSectionHeaderError")


# --- --sandbox-requirements files -----------------------------------------------------------


def test_sandbox_requirements_accept_requirements_and_index_options():
    text = textwrap.dedent(
        """\
        # for djgpt
        --extra-index-url https://download.pytorch.org/whl/cpu
        --index-url=https://pypi.org/simple
        -i https://pypi.org/simple
        openai==0.28  # the old library
        typer>=0.9,<0.10
        rich; python_version >= "3.8"
        torch @ https://example.invalid/torch-2.1.0-cp311-none-linux_x86_64.whl
        """
    )
    assert sandbox_requirements_problem(text) is None
    assert sandbox_requirement_lines(text) == [
        "--extra-index-url https://download.pytorch.org/whl/cpu",
        "--index-url=https://pypi.org/simple",
        "-i https://pypi.org/simple",
        "openai==0.28",
        "typer>=0.9,<0.10",
        'rich; python_version >= "3.8"',
        "torch @ https://example.invalid/torch-2.1.0-cp311-none-linux_x86_64.whl",
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("-r other.txt\n", "option -r is not allowed"),
        ("-c constraints.txt\n", "option -c is not allowed"),
        ("-e .\n", "option -e is not allowed"),
        ("--find-links /wheels\n", "option --find-links is not allowed"),
        ("--index-url /local/dir\n", "--index-url needs an http(s) URL"),
        ("--index-url https://user:token@pypi.example/simple\n", "credentials"),
        ("pkg @ https://token@example.invalid/pkg.whl\n", "credentials"),
        ("./libs/local_pkg\n", "local paths"),
        ("openai==0.28 \\\n", "continuation"),
        ("openai=0.28\n", "line 1 is not a valid requirement"),
        ("# nothing\n\n", "no requirements"),
        ("--extra-index-url https://download.pytorch.org/whl/cpu\n", "no requirements"),
    ],
)
def test_sandbox_requirements_problems(text, problem):
    found = sandbox_requirements_problem(text)
    assert found is not None and problem in found


def test_distribution_candidates():
    assert distribution_candidates("openai") == ("openai",)
    assert distribution_candidates("dotenv.main") == ("dotenv", "python-dotenv")
    assert distribution_candidates("PIL") == ("pil", "pillow")
    assert distribution_candidates("speech_recognition") == (
        "speech-recognition",
        "speechrecognition",
    )


# --- pixi ---------------------------------------------------------------------------------------

PIXI_FIXTURE = Path(__file__).parent / "fixtures" / "pixi_repo"
CONDA_FIXTURE = Path(__file__).parent / "fixtures" / "conda_env_repo"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pixi.toml", True),
        ("sub/pixi.toml", True),
        ("environment.yml", True),
        ("envs/environment.yaml", True),
        ("Environment.YML", True),
        ("environment-dev.yml", False),
        (".pre-commit-config.yaml", False),
    ],
)
def test_pixi_and_conda_files_are_dependency_files(path, expected):
    assert is_dependency_file(path) is expected


def test_pixi_pypi_dependencies_are_declared_in_every_table_at_their_own_lines():
    text = (PIXI_FIXTURE / "pyproject.toml").read_text()
    deps = parse_dependency_file("pyproject.toml", text)
    lines = text.splitlines()
    openai = [(d.line, d.clauses) for d in deps if d.name == "openai"]
    assert openai == [(25, (("==", "0.28"),)), (35, (("==", "0.27.8"),))]
    assert lines[24] == 'openai = "==0.28"'
    assert lines[34] == 'openai = { version = "==0.27.8" }'
    names = [d.name for d in deps]
    # conda tables are not pip requirements; the local path entry has no version
    assert "typer" not in names and "portaudio" not in names
    assert {"requests", "httpx", "pytest", "pyobjc-framework-cocoa", "voicebot"} <= set(names)


def test_standalone_pixi_toml():
    text = textwrap.dedent(
        """\
        [workspace]
        platforms = ["linux-64"]

        [dependencies]
        python = "3.11.*"
        numpy = ">=1.26"

        [pypi-dependencies]
        openai = "~=0.28.0"

        [target.linux-64.pypi-dependencies]
        uvloop = "*"

        [target.linux-aarch64.pypi-dependencies]
        aarch-only = "*"

        [feature.dev.target.unix.pypi-dependencies]
        pytest = ">=7"
        """
    )
    (dep,) = [d for d in parse_dependency_file("pixi.toml", text) if d.name == "openai"]
    assert dep.line == 9 and entirely_below(dep.clauses, "1.0")
    x86 = sandbox_pixi_targets("x86_64")
    reqs, left_out = install_requirements("pixi.toml", text, x86)
    assert reqs == [
        ("openai~=0.28.0", "pixi.toml [pypi-dependencies]"),
        ("uvloop", "pixi.toml [target.linux-64.pypi-dependencies]"),
        ("pytest>=7", "pixi.toml [feature.dev.target.unix.pypi-dependencies]"),
    ]
    assert [(n.name, n.why) for n in left_out] == [
        ("numpy", "conda-only package; not supported yet"),
        ("aarch-only", "pixi target linux-aarch64; the sandbox runs Linux"),
    ]
    arm = install_requirements("pixi.toml", text, sandbox_pixi_targets("arm64"))[0]
    assert ("aarch-only", "pixi.toml [target.linux-aarch64.pypi-dependencies]") in arm
    assert requirements_from_file("pixi.toml", text)[0] == "openai~=0.28.0"


def test_sandbox_pixi_targets():
    assert sandbox_pixi_targets("x86_64") == {"linux", "unix", "linux-64"}
    assert sandbox_pixi_targets("aarch64") == {"linux", "unix", "linux-aarch64"}
    assert sandbox_pixi_targets("ppc64le") == {"linux", "unix", "linux-ppc64le"}


def test_pixi_install_requirements_from_the_fixture():
    text = (PIXI_FIXTURE / "pyproject.toml").read_text()
    reqs, left_out = install_requirements("pyproject.toml", text, sandbox_pixi_targets("x86_64"))
    assert [r for r, _ in reqs] == [
        "openai==0.28",
        "requests>=2.31",
        "rich>=13",
        "pyobjc-framework-cocoa",  # filtered later, by the install plan (macOS only)
        "httpx<0.28",
        "pytest>=7",
    ]
    assert {(n.name, n.source) for n in left_out} == {
        ("typer", "pyproject.toml [tool.pixi.dependencies]"),
        ("rich", "pyproject.toml [tool.pixi.dependencies]"),
        ("portaudio", "pyproject.toml [tool.pixi.dependencies]"),
        ("pyobjc-core", "pyproject.toml [tool.pixi.target.osx-arm64.dependencies]"),
        ("openai", "pyproject.toml [tool.pixi.target.osx-arm64.pypi-dependencies]"),
    }


PIXI_EDIT = """\
[project]
name = "x"

[tool.pixi.pypi-dependencies]
{line}
"""


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ('openai = ">=1.55.3,<3"', None),
        ('openai = "*"', None),
        ('openai = { version = ">=1.0", extras = ["datalib"] }', None),
        ('app = { path = ".", editable = true }', None),
        ('openai = ">=1.0,"', "invalid version '>=1.0,'"),
        ('openai = "1.0"', "invalid version '1.0'"),
        ("openai = 1", "invalid version 1"),
        ('openai = { verison = ">=1" }', "unknown key(s) verison"),
    ],
)
def test_strict_check_of_pixi_pypi_dependencies(line, problem):
    found = dependency_file_problem("pyproject.toml", PIXI_EDIT.format(line=line))
    if problem is None:
        assert found is None
    else:
        assert found == (
            f"pyproject.toml [tool.pixi.pypi-dependencies] {line.split()[0]}: {problem}"
        )


def test_strict_check_covers_pixi_features_and_targets():
    text = '[feature.gpu.target.linux-64.pypi-dependencies]\nopenai = "=1.0"\n'
    assert dependency_file_problem("pixi.toml", text) == (
        "pixi.toml [feature.gpu.target.linux-64.pypi-dependencies] openai: invalid version '=1.0'"
    )


# --- conda environment files ----------------------------------------------------------------


def test_conda_environment_pip_list_is_declared_with_lines():
    text = (CONDA_FIXTURE / "environment.yml").read_text()
    deps = parse_dependency_file("environment.yml", text)
    assert [(d.name, d.line, d.col) for d in deps] == [
        ("openai", 10, 9),
        ("requests", 11, 9),
        ("pyobjc-core", 12, 9),
        ("pywin32", 13, 9),
    ]
    reqs, left_out = install_requirements("environment.yml", text)
    assert [r for r, _ in reqs] == [
        "openai==0.27.6",
        "requests>=2.31",
        "pyobjc-core==10.1",
        'pywin32; sys_platform == "win32"',
    ]
    assert {source for _, source in reqs} == {"environment.yml (pip)"}
    assert [(n.name, n.source, n.why) for n in left_out] == [
        ("numpy", "environment.yml (conda)", "conda-only package; not supported yet"),
        ("pandas", "environment.yml (conda)", "conda-only package; not supported yet"),
    ]


def test_conda_environment_yaml_subset():
    text = textwrap.dedent(
        """\
        ---
        # comment
        name: "my env"  # trailing comment
        channels: [conda-forge]
        """
    )
    with pytest.raises(ValueError, match="unsupported YAML at line 4"):
        parse_conda_environment(text)
    env = parse_conda_environment(
        textwrap.dedent(
            """\
            name: 'it''s'
            variables:
              OPENAI_LOG: debug
            dependencies:
            - python=3.11
            - "numpy >=1.24"
            - pip:
              - openai==0.28  # not a comment#inside
              - 'rich'
            """
        )
    )
    assert env.conda == (("python=3.11", 5, 3), ("numpy >=1.24", 6, 3))
    assert env.pip == (("openai==0.28", 8, 5), ("rich", 9, 5))
    assert not env.exported


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("dependencies: [a, b]\n", "dependencies must be a block list"),
        ("dependencies:\n  - pip: [openai]\n", "unsupported mapping under dependencies"),
        ("dependencies:\n  - a\n    - b\n", "inconsistent indentation under dependencies"),
        ("dependencies:\n  - pip:\n    - a\n      - b\n", "inconsistent indentation in the pip"),
        ("dependencies:\n\t- a\n", "tab indentation at line 2"),
        ("  - a\n", "content before any key"),
        ("dependencies:\n  - &anchor a\n", "unsupported YAML at line 2"),
        ("dependencies:\n  - 'a\n", "unterminated quoted string at line 2"),
        ("name\n", "expected a top-level key at line 1"),
        ("dependencies:\n  plain: x\n", "expected a list item under dependencies"),
    ],
)
def test_conda_environment_unsupported_input_raises_with_the_line(text, message):
    with pytest.raises(ValueError, match=message):
        parse_conda_environment(text)


def test_exported_conda_environments_are_recognised():
    exported = "name: x\ndependencies:\n  - zlib=1.2.13=h03a7124_4\n  - python=3.10.10\n"
    assert parse_conda_environment(exported).exported
    assert parse_conda_environment("name: x\nprefix: /opt/conda/envs/x\n").exported
    _, left_out = install_requirements("environment.yml", exported)
    assert [(n.name, n.why) for n in left_out] == [
        ("zlib", "exported conda environment (build strings or prefix:); not installed")
    ]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("dependencies:\n  - numpy>=1.24\n  - pip:\n    - openai>=1.55.3,<3\n", None),
        ("dependencies:\n  - pip:\n    - openai>=1.0,\n", "line 3 is not a valid pip requirement"),
        ("dependencies:\n  - numpy;x\n", "line 2 is not a valid conda package spec"),
        ("dependencies: [numpy]\n", "ValueError: dependencies must be a block list (line 1)"),
    ],
)
def test_strict_check_of_conda_environments(text, problem):
    found = dependency_file_problem("environment.yml", text)
    assert found is None if problem is None else (found is not None and problem in found)


def test_platform_only_packages():
    assert platform_only("pyobjc") == "macOS only"
    assert platform_only("PyObjC-Framework-Cocoa") == "macOS only"
    assert platform_only("appnope") == "macOS only"
    assert platform_only("pywin32") == "Windows only"
    assert platform_only("pyobjc-ish") == "macOS only"  # the pyobjc-* family
    assert platform_only("objc") is None and platform_only("requests") is None


def test_requirements_with_hashes_and_continuations_are_read():
    text = (
        "openai==0.28.1 \\\n"
        "    --hash=sha256:aaaa \\\n"
        "    --hash=sha256:bbbb\n"
        "rich==13.0 --hash=sha256:cccc  # pinned\n"
        "--require-hashes\n"
    )
    deps = parse_dependency_file("requirements.txt", text)
    assert [(d.name, d.clauses, d.line, d.raw) for d in deps] == [
        ("openai", (("==", "0.28.1"),), 1, "openai==0.28.1"),
        ("rich", (("==", "13.0"),), 4, "rich==13.0"),
    ]
