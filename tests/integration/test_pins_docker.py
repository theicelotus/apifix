"""A job whose only findings are pins: apifix sets them itself and verifies in a real sandbox.

Needs Docker and network. typing_extensions==4.5.0 would make pip pick an old openai 1.x,
which breaks on current httpx (the smoke check fails); the raised pin avoids that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apifix.engine.agent import AgentConfig, fix
from apifix.engine.docker import DockerCLI
from apifix.engine.sandbox import Sandbox, SandboxConfig, default_pip_cache_dir
from tests.integration.tiny_repo import make_repo
from tests.test_agent import FakeLLM

_usable, _why = DockerCLI().available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _usable, reason=_why or "no docker")]

REPO = {
    "requirements.txt": "openai==0.28.1\ntyping_extensions==4.5.0\npytest\n",
    "tests/test_imports.py": """\
        import openai


        def test_openai_imports():
            assert openai.__name__ == "openai"
        """,
}


class NoModel(FakeLLM):
    def preflight(self, require_tools: bool = True):
        raise AssertionError("nothing in this job needs the model")


@pytest.mark.parametrize("edit_mode", ["agent", "whole-file"])
def test_pin_only_job_is_verified_without_the_model(tmp_path: Path, edit_mode):
    repo = make_repo(tmp_path / "repo", REPO)
    sandbox = Sandbox(
        SandboxConfig(
            install_timeout_s=600,
            test_timeout_s=180,
            memory="1g",
            cpus=1.0,
            pip_cache_dir=default_pip_cache_dir(),
        )
    )
    llm = NoModel([])
    result = fix(
        repo, "openai-v1", llm=llm, sandbox=sandbox, config=AgentConfig(edit_mode=edit_mode)
    )
    assert llm.requests == []
    assert result.outcome in ("verified", "verified_weak"), result.report_markdown
    smoke = result.verification.fixed.smoke
    assert smoke is not None and smoke.status == "ok", smoke
    assert smoke.version is not None and smoke.version.startswith("1.")
    assert "+typing_extensions>=4.11,<5" in result.diff
