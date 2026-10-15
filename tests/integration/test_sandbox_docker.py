"""End-to-end sandbox runs against a real Docker daemon.

Skipped unless Docker is usable. Needs network for image pulls and the install phase.
Run just these with:  pytest -m docker
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from apifix.engine.cleanup import cleanup
from apifix.engine.docker import DockerCLI
from apifix.engine.migrations import get_migration
from apifix.engine.project import TestPlan
from apifix.engine.sandbox import WORKSPACE_PREFIX, Sandbox, SandboxConfig, run_sandbox

_usable, _why = DockerCLI().available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _usable, reason=_why or "no docker")]

CONFIG = SandboxConfig(install_timeout_s=300, test_timeout_s=120, memory="1g", cpus=1.0)


def make_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    return root


def apifix_containers() -> set[str]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--no-trunc", "--filter", "label=apifix=1", "-q"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.split())


@pytest.fixture
def no_leftover_containers():
    """Compare apifix containers before and after the test, so containers left over from an
    earlier crash (or another run) do not fail it. Call the returned check at the end."""
    before = apifix_containers()

    def check() -> None:
        leftover = apifix_containers() - before
        assert not leftover, f"containers not removed: {sorted(leftover)}"

    return check


SECURITY_TESTS = """
import os
import socket

import pytest


def test_runs_as_non_root():
    assert os.getuid() != 0


def test_host_env_is_not_visible():
    assert "APIFIX_CANARY_SECRET" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ


def test_network_is_off():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 53), timeout=3)


def test_work_copy_is_writable():
    with open("written-by-test.txt", "w") as f:
        f.write("ok")


def test_deps_are_read_only():
    with pytest.raises(OSError):
        open("/deps/venv/tampered.txt", "w")


def test_root_filesystem_is_read_only():
    with pytest.raises(OSError):
        open("/usr/tampered.txt", "w")
"""


def test_security_properties_hold_inside_the_sandbox(tmp_path, monkeypatch, no_leftover_containers):
    monkeypatch.setenv("APIFIX_CANARY_SECRET", "s3cr3t")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    repo = make_repo(tmp_path / "repo", {"tests/test_security.py": SECURITY_TESTS})
    result = run_sandbox(repo, CONFIG)
    assert result.status == "passed", (result.reason, result.test and result.test.stdout)
    assert result.counts.passed == 6
    assert not (repo / "written-by-test.txt").exists()  # only the copy was written
    no_leftover_containers()


def test_failing_tests_and_editable_install(tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {
            "pyproject.toml": """
            [build-system]
            requires = ["setuptools>=64"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "calc"
            version = "0.1"

            [tool.setuptools.packages.find]
            where = ["src"]
            """,
            "src/calc/__init__.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": """
            from calc import add

            def test_add():
                assert add(1, 2) == 3

            def test_broken():
                assert add(1, 1) == 3
            """,
        },
    )
    result = run_sandbox(repo, CONFIG)
    assert result.install_method == "editable", result.notes
    assert result.status == "failed"
    assert (result.counts.passed, result.counts.failed) == (1, 1)
    assert "tests.test_calc::test_broken" in {t.id for t in result.tests if t.outcome == "failed"}


def test_setuptools_scm_project_installs_without_git(tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {
            "pyproject.toml": """
            [build-system]
            requires = ["setuptools>=64", "setuptools-scm>=8"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "scmdemo"
            dynamic = ["version"]

            [tool.setuptools_scm]

            [tool.setuptools.packages.find]
            where = ["src"]
            """,
            "src/scmdemo/__init__.py": "",
            "tests/test_version.py": """
            from importlib.metadata import version

            def test_installed_with_pretend_version():
                assert version("scmdemo") == "0.0.0"
            """,
        },
    )
    result = run_sandbox(repo, CONFIG)
    assert result.install_method == "editable", result.notes
    assert result.status == "passed", (result.reason, result.test and result.test.stdout)


def test_unittest_repo(tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {
            "tests/test_u.py": """
            import unittest

            class T(unittest.TestCase):
                def test_ok(self):
                    self.assertTrue(True)

                @unittest.skip("not today")
                def test_skip(self):
                    pass
            """,
        },
    )
    result = run_sandbox(repo, CONFIG)
    assert result.runner == "pytest"  # detection runs unittest tests through pytest
    assert result.status == "passed", result.reason
    assert (result.counts.passed, result.counts.skipped) == (1, 1)


def test_unittest_runner_output_with_logging_and_docstrings(tmp_path):
    """The unittest runner (only used when a caller passes such a plan): a test that logs to
    stderr between `... ` and `ok`, and failures of tests with docstrings."""
    repo = make_repo(
        tmp_path / "repo",
        {
            "tests/test_u.py": """
            import logging
            import unittest

            logging.basicConfig(level=logging.INFO)

            class T(unittest.TestCase):
                def test_logs(self):
                    \"\"\"Logs on the way.\"\"\"
                    logging.info("working")
                    self.assertTrue(True)

                def test_fails(self):
                    \"\"\"Fails on purpose.\"\"\"
                    self.assertEqual(1, 2)
            """,
        },
    )
    plan = TestPlan(
        "unittest",
        ("tests/test_u.py",),
        ("{python}", "-m", "unittest", "discover", "-s", "tests", "-v"),
        "explicit",
    )
    result = Sandbox(CONFIG).run(repo, tests=plan)
    outcomes = {t.id.rsplit(".", 1)[-1]: t for t in result.tests}
    assert set(outcomes) == {"test_logs", "test_fails"}, result.test and result.test.stderr
    assert outcomes["test_logs"].outcome == "passed"
    assert "AssertionError: 1 != 2" in outcomes["test_fails"].message


def test_flat_package_layout_with_sibling_imports(tmp_path):
    """Synthetic layout: requirements at the root; code and a unittest file named
    test<name>.py side by side in a package dir; the test imports the code by bare name, logs
    on stderr, and one test needs a data file that is not in the repo."""
    repo = make_repo(
        tmp_path / "repo",
        {
            "requirements.txt": "",
            "svc/__init__.py": "",
            "svc/server.py": """
            import logging

            logging.basicConfig(level=logging.INFO)


            def reply(text):
                logging.info("replying")
                return text.upper()
            """,
            "svc/testserver.py": """
            import unittest

            from server import reply


            class ServerTest(unittest.TestCase):
                def test_reply(self):
                    \"\"\"Reply is upper case.\"\"\"
                    self.assertEqual(reply("hi"), "HI")

                def test_empty(self):
                    self.assertEqual(reply(""), "")

                def test_sample_file(self):
                    with open("data/sample.bin", "rb") as f:
                        self.assertTrue(f.read())
            """,
        },
    )
    result = run_sandbox(repo, CONFIG)
    assert result.import_paths == ["svc"]
    assert result.status == "failed", (result.reason, result.test and result.test.stdout)
    outcomes = {t.id.rsplit(":", 1)[-1]: t for t in result.tests}
    assert {k: t.outcome for k, t in outcomes.items()} == {
        "test_reply": "passed",
        "test_empty": "passed",
        "test_sample_file": "failed",
    }
    assert "FileNotFoundError" in outcomes["test_sample_file"].message


def test_smoke_check_reports_versions_and_limiting_pins(tmp_path):
    """openai releases before 1.55.3 cannot build a client with httpx 0.28 (exact pins, so the
    outcome does not depend on what is newest on the index)."""
    repo = make_repo(
        tmp_path / "repo",
        {
            "requirements.txt": "openai==1.5.0\nhttpx==0.28.1\ntyping_extensions==4.5.0\n"
            "pydantic==1.10.26\n",
            "tests/test_ok.py": "def test_ok():\n    assert True\n",
        },
    )
    result = Sandbox(CONFIG).run(repo, smoke=get_migration("openai-v1").smoke_check)
    assert result.status == "passed", result.reason
    smoke = result.smoke
    assert smoke is not None and smoke.status == "failed", smoke
    assert "unexpected keyword argument 'proxies'" in smoke.output
    assert smoke.version == "1.5.0" and smoke.requires.get("httpx") == "0.28.1"
    assert "typing_extensions==4.5.0" in smoke.pins and "httpx==0.28.1" in smoke.pins


def test_timeout_kills_and_removes_the_container(tmp_path, no_leftover_containers):
    repo = make_repo(
        tmp_path / "repo",
        {"tests/test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(600)\n"},
    )
    config = SandboxConfig(install_timeout_s=300, test_timeout_s=10)
    result = run_sandbox(repo, config)
    assert result.status == "timeout"
    no_leftover_containers()


def test_no_tests(tmp_path):
    repo = make_repo(tmp_path / "repo", {"app.py": "x = 1\n"})
    assert run_sandbox(repo, CONFIG).status == "unverified"


def test_cleanup_removes_an_orphaned_container_and_its_workspace(tmp_path):
    """What a power loss leaves behind: a labeled container whose owner is gone."""
    root = tmp_path / f"{WORKSPACE_PREFIX}orphan"
    (root / "deps").mkdir(parents=True)
    name = f"apifix-test-orphan-{tmp_path.name[-8:]}"
    subprocess.run(
        ["docker", "create", "--name", name, "--label", "apifix=1"]
        + ["--label", f"apifix.workspace={root}", f"python:{CONFIG.default_python}-slim"]
        + ["true"],
        check=True,
        capture_output=True,
    )
    try:
        report = cleanup(DockerCLI(), temp_dir=tmp_path, min_age_s=0)
        assert f"{name} (created)" in report.removed_containers
        assert report.removed_dirs == [str(root)]
        assert (
            name
            not in subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True
            ).stdout.split()
        )
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
