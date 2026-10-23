"""The split_config fixture's good migration in a real sandbox: the CLI keeps openai.api_key,
the call site uses the module-level client, and the tests set a placeholder key and patch
openai.chat.completions.create. This checks the guide's client.config advice against the real
openai 1.x (whole-file mode, scripted replies, all checks passing on the first reply).

Needs Docker and network.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apifix.engine.agent import AgentConfig, fix
from apifix.engine.docker import DockerCLI
from apifix.engine.sandbox import Sandbox, SandboxConfig, default_pip_cache_dir
from tests.test_agent import FakeLLM, block
from tests.test_unrelated import FIXTURE, GOOD_ASSISTANT, GOOD_CLI, GOOD_TESTS

_usable, _why = DockerCLI().available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _usable, reason=_why or "no docker")]


def test_module_level_client_migration_passes_with_real_openai(tmp_path: Path):
    repo = tmp_path / "split_config"
    shutil.copytree(FIXTURE, repo)
    sandbox = Sandbox(
        SandboxConfig(
            install_timeout_s=600,
            test_timeout_s=180,
            memory="1g",
            cpus=1.0,
            pip_cache_dir=default_pip_cache_dir(),
        )
    )
    # send order: non-test Python files, then test files (requirements.txt is set by apifix)
    llm = FakeLLM([block(GOOD_ASSISTANT), block(GOOD_CLI), block(GOOD_TESTS)])
    result = fix(
        repo,
        "openai-v1",
        llm=llm,
        sandbox=sandbox,
        config=AgentConfig(edit_mode="whole-file", max_retries=0),
    )
    assert len(llm.requests) == 3  # no check retry: the good migration passes every check
    assert result.outcome in ("verified", "verified_weak"), result.report_markdown
    v = result.verification
    assert v.fixed.counts.passed == 3 and v.fixed.counts.failed == v.fixed.counts.errors == 0
    smoke = v.fixed.smoke
    assert smoke is not None and smoke.status == "ok" and (smoke.version or "").startswith("1.")
    assert "+openai>=1.0,<2" in result.diff
    assert "-    openai.api_base = base_url\n+    openai.base_url = base_url" in result.diff
    assert "-    openai.api_key = api_key" not in result.diff  # kept as it was
