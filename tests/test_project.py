from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from apifix.engine.models import LibraryTarget
from apifix.engine.project import (
    PYTEST_COMMAND,
    SandboxRequirements,
    build_install_plan,
    ci_env_vars,
    defines_tests,
    detect_python_version,
    detect_tests,
    is_test_file,
    load_sandbox_requirements,
    pytest_config,
)


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return root


# --- Python version -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "version", "source"),
    [
        ({}, "3.11", "default"),
        ({".python-version": "3.10.12\n"}, "3.10", ".python-version"),
        ({".python-version": "# comment\n3.9\n"}, "3.9", ".python-version"),
        ({".python-version": "pypy3.9\n"}, "3.11", "default"),
        ({"runtime.txt": "python-3.8.18\n"}, "3.8", "runtime.txt"),
        (
            {"pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.8,<3.11"\n'},
            "3.10",
            "pyproject.toml requires-python",
        ),
        (
            {"pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.8"\n'},
            "3.11",
            "default (allowed by pyproject.toml requires-python)",
        ),
        (
            {"pyproject.toml": '[tool.poetry.dependencies]\npython = "^3.12"\n'},
            "3.14",
            "pyproject.toml [tool.poetry] python",
        ),
        (
            {"setup.py": 'from setuptools import setup\nsetup(python_requires=">=3.12")\n'},
            "3.14",
            "setup.py python_requires",
        ),
        (
            {"setup.cfg": "[options]\npython_requires = ~=3.9.0\n"},
            "3.9",
            "setup.cfg python_requires",
        ),
        # An explicit pin that contradicts requires-python is ignored.
        (
            {
                ".python-version": "3.7\n",
                "pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.9,<3.10"\n',
            },
            "3.9",
            "pyproject.toml requires-python",
        ),
    ],
)
def test_detect_python_version(tmp_path, files, version, source):
    choice = detect_python_version(write(tmp_path, files))
    assert (choice.version, choice.source) == (version, source)


def test_python_version_from_ci_matrix(tmp_path):
    write(
        tmp_path,
        {
            ".github/workflows/ci.yml": """
            jobs:
              test:
                strategy:
                  matrix:
                    python-version: ["3.8", "3.9", "3.10"]
                steps:
                  - uses: actions/setup-python@v5
                    with:
                      python-version: ${{ matrix.python-version }}
            """,
        },
    )
    choice = detect_python_version(tmp_path)
    assert (choice.version, choice.source) == ("3.10", ".github/workflows/ci.yml")


def test_python_version_from_ci_block_list(tmp_path):
    write(
        tmp_path,
        {
            ".github/workflows/test.yaml": """
            matrix:
              python-version:
                - 3.9
                - "3.12"
              os: [ubuntu-latest]
            """,
        },
    )
    assert detect_python_version(tmp_path).version == "3.12"


def test_python_version_file_beats_ci(tmp_path):
    write(
        tmp_path,
        {
            ".python-version": "3.9\n",
            ".github/workflows/ci.yml": "python-version: '3.12'\n",
        },
    )
    assert detect_python_version(tmp_path).version == "3.9"


def test_custom_default(tmp_path):
    assert detect_python_version(tmp_path, default="3.12").version == "3.12"


# --- test detection -------------------------------------------------------------------------


def test_no_tests(tmp_path):
    write(tmp_path, {"app.py": "x = 1\n"})
    plan = detect_tests(tmp_path)
    assert plan.runner is None and plan.test_files == () and plan.command == ()


def test_tests_in_venv_are_ignored(tmp_path):
    write(tmp_path, {".venv/lib/test_x.py": "", "app.py": ""})
    assert detect_tests(tmp_path).test_files == ()


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({"pytest.ini": "[pytest]\n"}, "pytest.ini"),
        ({"pyproject.toml": "[tool.pytest.ini_options]\n"}, "pyproject.toml pytest config"),
        ({"setup.cfg": "[tool:pytest]\n"}, "setup.cfg pytest config"),
        ({"tox.ini": "[pytest]\n"}, "tox.ini pytest config"),
        ({"tests/conftest.py": ""}, "conftest.py"),
        ({"requirements-dev.txt": "pytest==8.0\n"}, "pytest declared in requirements-dev.txt"),
    ],
)
def test_pytest_signals(tmp_path, files, reason):
    write(tmp_path, {"tests/test_a.py": "import unittest\n", **files})
    plan = detect_tests(tmp_path)
    assert plan.runner == "pytest" and plan.reason == reason
    assert plan.command == (*PYTEST_COMMAND, "tests/test_a.py")


def test_plain_test_functions_use_pytest(tmp_path):
    write(tmp_path, {"test_a.py": "def test_x():\n    assert True\n"})
    assert detect_tests(tmp_path).runner == "pytest"


def test_unittest_only_runs_with_pytest_and_explicit_files(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/tests.py": "import unittest\n",
            "pkg/test_b.py": "from unittest import TestCase\n",
            "pkg/conftest.py": "",
            "tests/helpers.py": "def make():\n    return 1\n",
        },
    )
    plan = detect_tests(tmp_path)
    assert plan.runner == "pytest"
    # conftest.py and helpers under tests/ are test files, but not given to pytest to collect
    assert plan.test_files == ("pkg/test_b.py", "pkg/tests.py")
    assert plan.command == (*PYTEST_COMMAND, "pkg/test_b.py", "pkg/tests.py")


def test_names_pytest_would_not_collect_are_passed_explicitly(tmp_path):
    write(
        tmp_path,
        {
            "svc/testserver.py": "import unittest\n",
            "checks.py": "import unittest\n\nclass ServerChecks(unittest.TestCase):\n    pass\n",
            "app.py": "def test_mode():\n    return False\n",  # no unittest/pytest import
        },
    )
    plan = detect_tests(tmp_path)
    assert plan.test_files == ("checks.py", "svc/testserver.py")


def test_configured_collection_is_kept(tmp_path):
    write(
        tmp_path,
        {
            "pytest.ini": "[pytest]\ntestpaths = tests\n",
            "tests/test_a.py": "",
            "examples/test_live.py": "",
        },
    )
    plan = detect_tests(tmp_path)
    assert plan.command == PYTEST_COMMAND
    assert plan.reason == "pytest.ini; collection as configured in pytest.ini"


# --- test files -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_a.py", True),
        ("test_a.py", True),
        ("pkg/a_test.py", True),
        ("tests/helpers.py", True),
        ("conftest.py", True),
        ("backend/testapp.py", True),  # unittest's discovery default: test*.py
        ("tests.py", True),
        ("test/helpers.py", True),
        ("src/app.py", False),
        ("contest.py", False),
        ("pkg/latest.py", False),
        ("tests/data.json", False),
    ],
)
def test_is_test_file_by_name(path, expected):
    assert is_test_file(path) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import unittest\n\nclass Checks(unittest.TestCase):\n    pass\n", True),
        ("from unittest import TestCase\n\nclass Checks(TestCase):\n    pass\n", True),
        ("import pytest\n\ndef test_x():\n    pass\n", True),
        ("import pytest\n\nasync def test_x():\n    pass\n", True),
        ("from unittest import mock\n\nclass TestServer:\n    pass\n", True),
        ("def test_x():\n    pass\n", False),  # imports neither unittest nor pytest
        ("import pytest\n\ndef helper():\n    pass\n", False),
        ("import unittest\n\ndef run():\n    def test_inner():\n        pass\n", False),
        ("import pytest\ndef test_(:\n", False),  # does not parse
    ],
)
def test_defines_tests(source, expected):
    assert defines_tests(source) is expected
    assert is_test_file("svc/checks.py", source) is expected


def test_is_test_file_reads_content_from_root(tmp_path):
    write(tmp_path, {"svc/checks.py": "import pytest\n\ndef test_x():\n    pass\n"})
    assert is_test_file("svc/checks.py", root=tmp_path)
    assert not is_test_file("svc/checks.py")
    assert not is_test_file("svc/missing.py", root=tmp_path)


# --- import paths -----------------------------------------------------------------------------

FLAT_SERVICE = {
    "requirements.txt": "openai==0.28.1\n",
    "svc/__init__.py": "",
    "svc/server.py": "def reply():\n    return 1\n",
    "svc/testserver.py": "import unittest\nfrom server import reply\n",
}


def test_sibling_import_in_a_package_gets_an_import_path(tmp_path):
    plan = detect_tests(write(tmp_path, FLAT_SERVICE))
    assert plan.import_paths == ("svc",)
    assert plan.notes == (
        "added svc/ to the import path: svc/testserver.py imports server from it",
    )
    assert plan.command == (*PYTEST_COMMAND, "svc/testserver.py")


def test_module_in_a_parent_dir_is_found(tmp_path):
    files = {
        **FLAT_SERVICE,
        "svc/checks/__init__.py": "",
        "svc/checks/test_x.py": "import server\n",
    }
    del files["svc/testserver.py"]
    assert detect_tests(write(tmp_path, files)).import_paths == ("svc",)


def test_no_import_path_when_pytest_already_has_it(tmp_path):
    # no __init__.py: pytest puts svc/ itself on sys.path
    files = {k: v for k, v in FLAT_SERVICE.items() if k != "svc/__init__.py"}
    assert detect_tests(write(tmp_path, files)).import_paths == ()


@pytest.mark.parametrize("where", ["server.py", "src/server.py", "src/server/__init__.py"])
def test_no_import_path_when_importable_from_the_root(tmp_path, where):
    write(tmp_path, {**FLAT_SERVICE, where: ""})
    assert detect_tests(tmp_path).import_paths == ()


def test_stdlib_and_third_party_imports_add_nothing(tmp_path):
    files = {**FLAT_SERVICE, "svc/testserver.py": "import json\nimport flask\nimport unittest\n"}
    assert detect_tests(write(tmp_path, files)).import_paths == ()


def test_repo_pythonpath_config_replaces_the_guess(tmp_path):
    files = {**FLAT_SERVICE, "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["lib"]\n'}
    plan = detect_tests(write(tmp_path, files))
    assert plan.import_paths == ("lib",)
    assert plan.notes == ("import path from pyproject.toml pythonpath: lib",)
    assert plan.command == (*PYTEST_COMMAND, "svc/testserver.py")  # no testpaths: explicit


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({}, (None, {})),
        (
            {"pytest.ini": "[pytest]\npythonpath = a b\n"},
            ("pytest.ini", {"pythonpath": ["a", "b"]}),
        ),
        (
            {"setup.cfg": "[tool:pytest]\ntestpaths = tests\npython_files = check_*.py\n"},
            ("setup.cfg", {"testpaths": ["tests"], "python_files": ["check_*.py"]}),
        ),
        ({"tox.ini": "[pytest]\naddopts = -q\n"}, ("tox.ini", {})),
        (
            {"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = "tests"\n'},
            ("pyproject.toml", {"testpaths": ["tests"]}),
        ),
        # pytest.ini wins over pyproject.toml
        (
            {
                "pytest.ini": "[pytest]\n",
                "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["x"]\n',
            },
            ("pytest.ini", {}),
        ),
    ],
)
def test_pytest_config(tmp_path, files, expected):
    assert pytest_config(write(tmp_path, files)) == expected


# --- install plan ---------------------------------------------------------------------------


def test_install_plan_collects_everything(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": """
            [build-system]
            requires = ["hatchling>=1.18"]
            build-backend = "hatchling.build"

            [project]
            name = "My_App"
            dependencies = ["openai==0.28.1", "httpx; python_version >= '3.8'"]

            [project.optional-dependencies]
            test = ["pytest-asyncio", "my-app[extra]"]
            """,
            "requirements-dev.txt": """
            -r requirements.txt
            -e .
            ./libs/local_pkg
            black==24.1.0  # formatter
            --index-url https://example.invalid/simple
            """,
            "tests/test_a.py": "",
        },
    )
    plan = build_install_plan(tmp_path, "pytest")
    assert plan.requirements == (
        "openai==0.28.1",
        "httpx; python_version >= '3.8'",
        "pytest-asyncio",
        "black==24.1.0",
        "hatchling>=1.18",
        "editables",
        "pytest",
    )
    assert plan.sources == ("pyproject.toml", "requirements-dev.txt")
    assert any("self-references to my-app dropped" in n for n in plan.notes)
    assert plan.lockfile is None and plan.constraints == ()


def test_install_plan_setup_py_defaults_to_setuptools(tmp_path):
    write(
        tmp_path,
        {
            "setup.py": """
            from setuptools import setup
            setup(
                name="legacy",
                install_requires=["requests>=2"],
                extras_require={"dev": ["pytest>=7"]},
            )
            """,
        },
    )
    plan = build_install_plan(tmp_path, "pytest")
    assert plan.requirements == ("requests>=2", "pytest>=7", "setuptools", "wheel")


def test_install_plan_poetry_and_pipfile(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": """
            [tool.poetry.dependencies]
            python = "^3.9"
            openai = "^0.28"
            uvicorn = { version = "^0.23", extras = ["standard"] }
            pywin32 = { version = "*", markers = "sys_platform == 'win32'" }
            mylib = { path = "../mylib" }

            [tool.poetry.group.dev.dependencies]
            pytest = "*"

            [build-system]
            requires = ["poetry-core"]
            build-backend = "poetry.core.masonry.api"
            """,
            "Pipfile": """
            [packages]
            requests = "==2.31.0"
            local = { path = ".", editable = true }
            """,
        },
    )
    plan = build_install_plan(tmp_path, "pytest")
    assert plan.requirements == (
        "openai>=0.28,<0.29",
        "uvicorn[standard]>=0.23,<0.24",
        "pywin32; sys_platform == 'win32'",
        "pytest",
        "requests==2.31.0",
        "poetry-core",
    )


def test_install_plan_unreadable_manifest_is_noted(tmp_path):
    write(tmp_path, {"pyproject.toml": "[project\n", "requirements.txt": "requests\n"})
    plan = build_install_plan(tmp_path, None)
    assert "requests" in plan.requirements
    assert any(n.startswith("pyproject.toml: could not parse") for n in plan.notes)


# --- lockfiles ------------------------------------------------------------------------------


def test_poetry_lock_pins(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": '[tool.poetry.dependencies]\nopenai = "^0.28"\n',
            "poetry.lock": """
            [[package]]
            name = "openai"
            version = "0.28.1"

            [[package]]
            name = "colorama"
            version = "0.4.6"
            markers = "sys_platform == 'win32'"

            [[package]]
            name = "mylib"
            version = "0.1.0"
            [package.source]
            type = "directory"
            url = "../mylib"
            """,
        },
    )
    plan = build_install_plan(tmp_path, None)
    assert plan.lockfile == "poetry.lock"
    assert plan.constraints == ("openai==0.28.1", "colorama==0.4.6; sys_platform == 'win32'")


def test_pipfile_lock_pins(tmp_path):
    lock = {
        "default": {
            "requests": {"version": "==2.31.0", "markers": "python_version >= '3.7'"},
            "local": {"path": ".", "editable": True},
        },
        "develop": {"pytest": {"version": "==8.0.0"}},
    }
    write(tmp_path, {"Pipfile": '[packages]\nrequests = "*"\n'})
    (tmp_path / "Pipfile.lock").write_text(json.dumps(lock))
    plan = build_install_plan(tmp_path, None)
    assert plan.lockfile == "Pipfile.lock"
    assert plan.constraints == (
        "requests==2.31.0; python_version >= '3.7'",
        "pytest==8.0.0",
    )


def test_fully_pinned_requirements_act_as_lock(tmp_path):
    write(
        tmp_path,
        {
            "requirements.txt": "openai==0.28.1\nrequests[socks]==2.31.0\n",
            "requirements-dev.txt": "pytest>=7\n",
        },
    )
    plan = build_install_plan(tmp_path, None)
    assert plan.lockfile == "requirements.txt (fully pinned)"
    assert plan.constraints == ("openai==0.28.1", "requests==2.31.0")


def test_partly_pinned_requirements_are_not_a_lock(tmp_path):
    write(tmp_path, {"requirements.txt": "openai==0.28.1\nrequests\n"})
    assert build_install_plan(tmp_path, None).lockfile is None


def test_stale_lock_pin_is_dropped_after_a_bump(tmp_path):
    # The fix bumped openai, but poetry.lock still pins 0.28.1 (it cannot be relocked offline).
    write(
        tmp_path,
        {
            "pyproject.toml": '[tool.poetry.dependencies]\nopenai = "^1.3"\n',
            "poetry.lock": """
            [[package]]
            name = "openai"
            version = "0.28.1"

            [[package]]
            name = "tqdm"
            version = "4.66.1"
            """,
        },
    )
    plan = build_install_plan(tmp_path, None)
    assert plan.constraints == ("tqdm==4.66.1",)
    assert any("openai==0.28.1 conflicts" in n for n in plan.notes)


def test_corrupt_lock_is_noted(tmp_path):
    write(tmp_path, {"poetry.lock": "[[package]\n"})
    plan = build_install_plan(tmp_path, None)
    assert plan.lockfile is None
    assert any("poetry.lock unreadable" in n for n in plan.notes)


# --- environment variables the repo's CI sets ---------------------------------------------------


def _workflow(tmp_path, name: str, text: str):
    wf = tmp_path / ".github" / "workflows" / name
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(textwrap.dedent(text))


@pytest.mark.parametrize(
    "text",
    [
        """
        jobs:
          test:
            env:
              OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        """,
        """
        env:
          OPENAI_API_KEY: sk-dummy
        """,
        """
        steps:
          - run: echo "OPENAI_API_KEY=dummy" >> $GITHUB_ENV
        """,
        """
        steps:
          - run: OPENAI_API_KEY=x pytest
        """,
    ],
)
def test_ci_env_vars_found(tmp_path, text):
    _workflow(tmp_path, "ci.yml", text)
    assert ci_env_vars(tmp_path, ["OPENAI_API_KEY"]) == {
        "OPENAI_API_KEY": ".github/workflows/ci.yml"
    }


@pytest.mark.parametrize(
    "text",
    [
        "# OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}\n",
        "env:\n  MY_OPENAI_API_KEY: x\n",
        "run: echo $OPENAI_API_KEY\n",
        "run: echo ${OPENAI_API_KEY}=x\n",
    ],
)
def test_ci_env_vars_not_found(tmp_path, text):
    _workflow(tmp_path, "ci.yaml", text)
    assert ci_env_vars(tmp_path, ["OPENAI_API_KEY"]) == {}


def test_ci_env_vars_without_workflows_or_names(tmp_path):
    assert ci_env_vars(tmp_path, ["OPENAI_API_KEY"]) == {}
    _workflow(tmp_path, "a.yml", "env:\n  OPENAI_API_KEY: x\n")
    _workflow(tmp_path, "b.yml", "env:\n  OPENAI_API_KEY: y\n")
    assert ci_env_vars(tmp_path, []) == {}
    assert ci_env_vars(tmp_path, ["OPENAI_API_KEY"]) == {
        "OPENAI_API_KEY": ".github/workflows/a.yml"
    }


# --- declared dependencies and --sandbox-requirements ---------------------------------------


def test_install_plan_records_where_each_dependency_is_declared(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": """
            [project]
            name = "app"
            dependencies = ["openai==0.28.1", "Python_Dotenv>=1"]
            """,
            "requirements.txt": "openai==0.28.1\nrich\n",
        },
    )
    plan = build_install_plan(tmp_path, "pytest")
    assert plan.declared == {
        "openai": ("pyproject.toml", "requirements.txt"),
        "python-dotenv": ("pyproject.toml",),
        "rich": ("requirements.txt",),
    }
    assert plan.override is None and plan.not_installed == ()


def test_load_sandbox_requirements(tmp_path):
    path = tmp_path / "sandbox-reqs.txt"
    path.write_text("# mine\nopenai==0.28\n--extra-index-url https://example.invalid/simple\n")
    req = load_sandbox_requirements(path)
    assert req.lines == ("openai==0.28", "--extra-index-url https://example.invalid/simple")
    assert req.count == 1 and len(req.sha256) == 64
    assert req.label == "--sandbox-requirements sandbox-reqs.txt"
    (tmp_path / "bad.txt").write_text("-r other.txt\n")
    with pytest.raises(ValueError, match="bad.txt: line 1: option -r is not allowed"):
        load_sandbox_requirements(tmp_path / "bad.txt")
    with pytest.raises(ValueError, match="cannot read --sandbox-requirements"):
        load_sandbox_requirements(tmp_path / "missing.txt")


OVERRIDE = SandboxRequirements(
    "/home/u/reqs.txt",
    "0" * 64,
    ("--extra-index-url https://example.invalid/simple", "openai==0.28", "typer", "demo"),
)
PIXI_LIKE = {
    "pyproject.toml": """
    [project]
    name = "demo"
    dependencies = []

    [build-system]
    requires = ["hatchling"]
    build-backend = "hatchling.build"
    """,
}


def test_override_replaces_detection(tmp_path):
    repo = write(tmp_path / "repo", {**PIXI_LIKE, "requirements.txt": "flask==2.0\n"})
    plan = build_install_plan(repo, "pytest", override=OVERRIDE)
    assert plan.requirements == (
        "--extra-index-url https://example.invalid/simple",
        "openai==0.28",
        "typer",
        "hatchling",
        "editables",
        "pytest",
    )
    assert plan.constraints == () and plan.lockfile is None  # requirements.txt is fully pinned
    assert plan.sources == ("--sandbox-requirements reqs.txt",)
    assert plan.override == "--sandbox-requirements reqs.txt" and plan.replaced == ()
    assert plan.declared == {
        "openai": ("--sandbox-requirements reqs.txt",),
        "typer": ("--sandbox-requirements reqs.txt",),
        "demo": ("--sandbox-requirements reqs.txt",),
    }
    assert "dependencies from --sandbox-requirements reqs.txt (3 requirement(s))" in plan.notes[0]
    assert any("self-references to demo dropped" in n for n in plan.notes)


def test_override_takes_the_fixs_declaration_for_every_name_the_fix_changed(tmp_path):
    files = {**PIXI_LIKE, "requirements.txt": "openai==0.28\ntyper\nhttpx==0.27.0\n"}
    original = write(tmp_path / "original", files)
    fixed = write(
        tmp_path / "fixed",
        {**files, "requirements.txt": "openai>=1.0\ntyper\nrequests>=2\n"},
    )
    plan = build_install_plan(fixed, "pytest", override=OVERRIDE, original=original)
    assert plan.requirements[:4] == (
        "--extra-index-url https://example.invalid/simple",
        "typer",  # unchanged by the fix: the user's line stays
        "openai>=1.0",
        "requests>=2",
    )
    assert "httpx==0.27.0" not in plan.requirements  # removed by the fix, never in the file
    assert plan.replaced == (
        "httpx: not listed -> removed by the fix",
        "openai: openai==0.28 -> openai>=1.0",
        "requests: not listed -> requests>=2",
    )
    assert plan.declared["openai"] == ("requirements.txt",)
    assert plan.declared["typer"] == ("--sandbox-requirements reqs.txt",)
    # the original code gets the user's file unchanged
    base = build_install_plan(original, "pytest", override=OVERRIDE)
    assert "openai==0.28" in base.requirements and base.replaced == ()


TARGET = LibraryTarget("openai", "1.0", ">=1.0,<2")


def test_override_old_library_pin_gets_the_target_when_the_fix_did_not_change_it(tmp_path):
    # SuspensePerception: the repo's declaration stays as it was, the user's file pins 0.x
    files = {**PIXI_LIKE, "requirements.txt": "openai\ntyper\n"}
    original = write(tmp_path / "original", files)
    fixed = write(tmp_path / "fixed", {**files, "app.py": "import openai\n"})
    plan = build_install_plan(fixed, "pytest", override=OVERRIDE, original=original, target=TARGET)
    assert "openai>=1.0,<2" in plan.requirements and "openai==0.28" not in plan.requirements
    assert plan.upgraded == ("openai==0.28 -> openai>=1.0,<2",) and plan.replaced == ()
    assert plan.declared["openai"] == ("--sandbox-requirements reqs.txt",)
    # the original code gets the user's line unchanged, target or not
    base = build_install_plan(original, "pytest", override=OVERRIDE, target=TARGET)
    assert "openai==0.28" in base.requirements and base.upgraded == ()


def test_override_library_pin_the_fix_changed_takes_the_fixs_line(tmp_path):
    files = {**PIXI_LIKE, "requirements.txt": "openai\n"}
    original = write(tmp_path / "original", files)
    fixed = write(tmp_path / "fixed", {**files, "requirements.txt": "openai>=1.0,<2\n"})
    plan = build_install_plan(fixed, "pytest", override=OVERRIDE, original=original, target=TARGET)
    assert plan.replaced == ("openai: openai==0.28 -> openai>=1.0,<2",)
    assert plan.upgraded == ()


@pytest.mark.parametrize(
    ("line", "upgraded"),
    [
        ("openai==0.28.1", "openai>=1.0,<2"),
        ("openai", "openai>=1.0,<2"),
        (
            "OpenAI[datalib]>=0.27 ; python_version < '3.12'",
            "OpenAI[datalib]>=1.0,<2; python_version < '3.12'",
        ),
        ("openai>=1.3", None),
        ("openai @ https://example.invalid/openai-0.28.1-py3-none-any.whl", None),
    ],
)
def test_override_library_lines_that_allow_0x_are_upgraded(tmp_path, line, upgraded):
    override = SandboxRequirements("/home/u/reqs.txt", "0" * 64, (line, "numpy"))
    original = write(tmp_path / "original", PIXI_LIKE)
    fixed = write(tmp_path / "fixed", {**PIXI_LIKE, "app.py": "x = 1\n"})
    plan = build_install_plan(fixed, "pytest", override=override, original=original, target=TARGET)
    expected = upgraded or line
    assert plan.requirements[:2] == (expected, "numpy")
    assert plan.upgraded == ((f"{line} -> {upgraded}",) if upgraded else ())


# --- pixi and conda environment files ---------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_install_plan_for_a_pixi_project(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    plan = build_install_plan(FIXTURES_DIR / "pixi_repo", "pytest")
    assert plan.requirements == (
        "openai==0.28",
        "requests>=2.31",
        "rich>=13",
        "httpx<0.28",
        "pytest>=7",
        "hatchling",
        "editables",
    )
    assert plan.sources == (
        "pyproject.toml",
        "pyproject.toml [tool.pixi.pypi-dependencies]",
        "pyproject.toml [tool.pixi.target.linux.pypi-dependencies]",
        "pyproject.toml [tool.pixi.feature.test.pypi-dependencies]",
    )
    assert plan.declared["openai"] == ("pyproject.toml [tool.pixi.pypi-dependencies]",)
    assert plan.notes == (
        "environment.yml: not installed (the repo has a pixi manifest, which does not read it)",
    )
    conda = "conda-only package; not supported yet"
    assert [(n.name, n.source, n.why) for n in plan.not_installed] == [
        ("pyobjc-framework-cocoa", "pyproject.toml [tool.pixi.pypi-dependencies]", "macOS only"),
        ("typer", "pyproject.toml [tool.pixi.dependencies]", conda),
        ("portaudio", "pyproject.toml [tool.pixi.dependencies]", conda),
        (
            "pyobjc-core",
            "pyproject.toml [tool.pixi.target.osx-arm64.dependencies]",
            "pixi target osx-arm64; the sandbox runs Linux",
        ),
        (
            "typer",
            "environment.yml (conda)",
            "the pixi manifest takes precedence; pixi does not read this file",
        ),
    ]  # rich and openai are installed from the pixi pypi table, so they are not listed


def test_install_plan_for_a_conda_environment():
    plan = build_install_plan(FIXTURES_DIR / "conda_env_repo", "pytest")
    assert plan.requirements == (
        "openai==0.27.6",
        "requests>=2.31",
        'pywin32; sys_platform == "win32"',  # pip skips it by its marker
        "pytest",
    )
    assert plan.sources == ("environment.yml", "environment.yml (pip)")
    assert [(n.name, n.why) for n in plan.not_installed] == [
        ("pyobjc-core", "macOS only"),
        ("numpy", "conda-only package; not supported yet"),
        ("pandas", "conda-only package; not supported yet"),
    ]


def test_environment_yml_yields_to_other_sources(tmp_path):
    write(
        tmp_path,
        {
            "requirements.txt": "openai>=1.0\n",
            "environment.yml": """
            name: x
            dependencies:
              - pip:
                - openai==0.27.6
                - rich
            """,
        },
    )
    plan = build_install_plan(tmp_path, None)
    assert plan.requirements == ("openai>=1.0", "rich")
    assert plan.notes == (
        "environment.yml (pip): openai==0.27.6 not installed (requirements.txt declares openai)",
    )


def test_exported_conda_lists_are_not_installed_but_their_pip_list_is(tmp_path):
    write(
        tmp_path,
        {
            "environment.yml": """
            name: x
            dependencies:
              - python=3.10.10=h3ba56d0_0_cpython
              - spotipy=2.23.0=pyhd8ed1ab_0
              - pip:
                - openai==0.27.6
            prefix: /Users/someone/miniforge3/envs/x
            """,
        },
    )
    plan = build_install_plan(tmp_path, None)
    assert plan.requirements == ("openai==0.27.6",)
    assert [(n.name, n.why) for n in plan.not_installed] == [
        ("spotipy", "exported conda environment (build strings or prefix:); not installed")
    ]


def test_unreadable_environment_yml_is_skipped_with_a_note(tmp_path):
    write(tmp_path, {"environment.yml": "dependencies: [openai]\n"})
    plan = build_install_plan(tmp_path, None)
    assert plan.requirements == () and plan.sources == ()
    assert plan.notes == ("environment.yml: could not parse (ValueError); skipped",)


def test_packages_apifix_installs_itself_are_not_listed_as_left_out(tmp_path):
    write(
        tmp_path,
        {
            "pyproject.toml": """
            [tool.pixi.dependencies]
            pytest = ">=7"
            typer = "*"
            """,
        },
    )
    plan = build_install_plan(tmp_path, "pytest")
    assert "pytest" in plan.requirements
    assert [n.name for n in plan.not_installed] == ["typer"]
