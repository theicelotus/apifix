"""Region edits in a real sandbox: a test file over the region threshold is sent as regions,
the scripted reply's labelled blocks are spliced back, and the spliced file runs against the
real openai 1.x (whole-file mode, all checks passing on the first reply).

Needs Docker and network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apifix.engine.agent import AgentConfig, fix
from apifix.engine.docker import DockerCLI
from apifix.engine.sandbox import Sandbox, SandboxConfig, default_pip_cache_dir
from tests.test_agent import FakeLLM, block
from tests.test_region_edits import GOOD_REGIONS, TESTS_BIG
from tests.test_verifier import APP_NEW, ORIGINAL, write_tree

_usable, _why = DockerCLI().available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _usable, reason=_why or "no docker")]


def test_region_edited_test_file_passes_with_real_openai(tmp_path: Path):
    repo = write_tree(tmp_path / "repo", {**ORIGINAL, "tests/test_app.py": TESTS_BIG})
    sandbox = Sandbox(
        SandboxConfig(
            install_timeout_s=600,
            test_timeout_s=180,
            memory="1g",
            cpus=1.0,
            pip_cache_dir=default_pip_cache_dir(),
        )
    )
    llm = FakeLLM([block(APP_NEW), GOOD_REGIONS])  # app.py whole, the test file as regions
    result = fix(
        repo,
        "openai-v1",
        llm=llm,
        sandbox=sandbox,
        config=AgentConfig(edit_mode="whole-file", max_retries=0),
    )
    assert len(llm.requests) == 2, result.report_markdown
    assert result.outcome in ("verified", "verified_weak"), result.report_markdown
    entry = result.trajectory["attempts"][0]["files"][1]
    assert (entry["form"], entry["accepted"]) == ("regions", True)
    v = result.verification
    assert v.fixed.counts.passed == 27 and v.fixed.counts.failed == v.fixed.counts.errors == 0
    assert '+@mock.patch("app.client.chat.completions.create")' in result.diff
    assert "test_extra" not in result.diff
