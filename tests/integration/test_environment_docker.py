"""A pixi project in real sandboxes: pip dependencies come from [tool.pixi.pypi-dependencies],
a conda-only package the tests need makes the environment incomplete, and a
--sandbox-requirements file fills the gap while the fix's own pin change still applies.

Needs Docker and network (installs openai 0.28.1 for the baseline and 1.x for the fix).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apifix.engine.docker import DockerCLI
from apifix.engine.project import load_sandbox_requirements
from apifix.engine.sandbox import Sandbox, SandboxConfig, default_pip_cache_dir
from apifix.engine.verifier import verify

_usable, _why = DockerCLI().available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _usable, reason=_why or "no docker")]

CONFIG = dict(
    install_timeout_s=600,
    test_timeout_s=180,
    memory="1g",
    cpus=1.0,
    pip_cache_dir=default_pip_cache_dir(),
)

PYPROJECT = """\
    [project]
    name = "tinyenv"
    version = "0.1.0"
    dependencies = []

    [build-system]
    requires = ["setuptools>=61"]
    build-backend = "setuptools.build_meta"

    [tool.setuptools]
    py-modules = ["app"]

    [tool.pixi.workspace]
    channels = ["conda-forge"]
    platforms = ["linux-64", "osx-arm64"]

    [tool.pixi.dependencies]
    python = "3.11.*"
    tomli_w = "*"

    [tool.pixi.target.osx-arm64.pypi-dependencies]
    appnope = "*"

    [tool.pixi.pypi-dependencies]
    openai = "==0.28.1"
    tinyenv = { path = ".", editable = true }
    """

ORIGINAL = {
    "pyproject.toml": PYPROJECT,
    "app.py": """\
        import openai


        def ask(question):
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini", messages=[{"role": "user", "content": question}]
            )
            return response["choices"][0]["message"]["content"].strip()
        """,
    "tests/test_app.py": """\
        from unittest import mock

        import tomli_w

        from app import ask


        @mock.patch("openai.ChatCompletion.create")
        def test_ask(create):
            create.return_value = {"choices": [{"message": {"content": " hi "}}]}
            assert ask("q") == "hi"


        def test_settings_render():
            assert tomli_w.dumps({"a": 1}) == "a = 1\\n"
        """,
}

FIXED = {
    "pyproject.toml": PYPROJECT.replace('openai = "==0.28.1"', 'openai = ">=1.55.3,<2"'),
    "app.py": """\
        import openai

        client = openai.OpenAI(api_key="unused-in-tests")


        def ask(question):
            response = client.chat.completions.create(
                model="gpt-4o-mini", messages=[{"role": "user", "content": question}]
            )
            return response.choices[0].message.content.strip()
        """,
    "tests/test_app.py": """\
        from types import SimpleNamespace as NS
        from unittest import mock

        import tomli_w

        from app import ask


        @mock.patch("app.client.chat.completions.create")
        def test_ask(create):
            create.return_value = NS(choices=[NS(message=NS(content=" hi "))])
            assert ask("q") == "hi"


        def test_settings_render():
            assert tomli_w.dumps({"a": 1}) == "a = 1\\n"
        """,
}


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    return root


@pytest.fixture(scope="module")
def trees(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("pixi")
    return write(root / "original", ORIGINAL), write(root / "fixed", FIXED)


def test_conda_only_test_dependency_makes_the_environment_incomplete(trees):
    original, fixed = trees
    v = verify(original, fixed, "openai-v1", sandbox=Sandbox(SandboxConfig(**CONFIG)))
    assert v.baseline.declared["openai"] == ["pyproject.toml [tool.pixi.pypi-dependencies]"]
    assert v.fixed.smoke is not None and v.fixed.smoke.status == "ok"  # openai 1.x installed
    assert v.verdict == "unverified", v.reasons
    (r,) = [r for r in v.reasons if r.code == "environment_incomplete"]
    assert (
        "tomli_w (pyproject.toml [tool.pixi.dependencies]: not installed (conda-only package; "
        "not supported yet))" in r.message
    )
    md = v.report_markdown()
    assert "### Declared dependencies not installed" in md
    assert "`pyproject.toml [tool.pixi.target.osx-arm64.pypi-dependencies]`: appnope" in md


def test_sandbox_requirements_fill_the_gap_and_the_fixs_pin_still_applies(trees, tmp_path):
    original, fixed = trees
    reqs = tmp_path / "sandbox-requirements.txt"
    reqs.write_text("openai==0.28.1\ntomli-w\n")
    config = SandboxConfig(**CONFIG, requirements_override=load_sandbox_requirements(reqs))
    v = verify(original, fixed, "openai-v1", sandbox=Sandbox(config))
    assert v.baseline.counts.passed == 2, v.baseline.reason
    assert v.fixed.override_replaced == ["openai: openai==0.28.1 -> openai>=1.55.3,<2"]
    assert v.verdict == "verified", v.reasons
