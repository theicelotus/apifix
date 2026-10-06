"""The fix agent against the real Claude API and a real sandbox, on the tiny openai 0.28 repo.

Opt-in: `pytest --llm --docker -m llm`. Costs real money (capped at $1 per run) and needs
ANTHROPIC_API_KEY, Docker and network.
"""

from __future__ import annotations

import json
import os

import pytest

from apifix.engine.agent import AgentConfig, fix
from apifix.engine.docker import DockerCLI
from apifix.engine.llm import get_llm
from apifix.engine.sandbox import Sandbox, SandboxConfig
from tests.integration.tiny_repo import ORIGINAL, make_repo

_docker, _why = DockerCLI().available()
pytestmark = [
    pytest.mark.llm,
    pytest.mark.docker,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY unset"),
    pytest.mark.skipif(not _docker, reason=_why or "no docker"),
]

SANDBOX = SandboxConfig(install_timeout_s=600, test_timeout_s=180, memory="1g", cpus=1.0)


def test_live_fix_of_the_tiny_repo(tmp_path):
    repo = make_repo(tmp_path / "repo", ORIGINAL)
    result = fix(
        repo,
        "openai-v1",
        llm=get_llm("anthropic"),
        sandbox=Sandbox(SANDBOX),
        config=AgentConfig(max_cost_usd=1.0, max_retries=2),
    )
    (tmp_path / "trajectory.json").write_text(json.dumps(result.trajectory, indent=2))
    assert result.outcome in ("verified", "verified_weak"), (
        result.reason,
        result.report_markdown,
        tmp_path / "trajectory.json",
    )
    assert "chat.completions" in result.diff
    assert result.verification.fixed.counts.passed == 2
    assert result.cost_usd <= 1.0
    assert all(r["model"] for a in result.trajectory["attempts"] for r in a["responses"])
