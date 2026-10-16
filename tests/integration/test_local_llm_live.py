"""The fix agent against a local Ollama (OpenAI-compatible endpoint).

Opt-in: `pytest --local-llm -m local_llm -s` (add `--docker` for the full fix). Skipped unless
an Ollama server answers at $APIFIX_BASE_URL (default http://localhost:11434/v1) with the model
$APIFIX_LOCAL_MODEL (default qwen2.5-coder:7b) installed. Free, but slow on CPU.

The full-fix tests (agent mode and whole-file mode) only assert that the job completes cleanly:
a small local model may well not reach `verified`. Their verdict, attempts and token usage are
printed (`-s`) and attached to the test report (`-rA` shows them) so results are visible.
"""

from __future__ import annotations

import json
import os

import pytest

from apifix.engine.agent import AgentConfig, fix
from apifix.engine.docker import DockerCLI
from apifix.engine.llm import LLMError, Usage
from apifix.engine.llm_openai_compat import DEFAULT_BASE_URL, OpenAICompatLLM
from apifix.engine.sandbox import Sandbox, SandboxConfig
from tests.integration.tiny_repo import ORIGINAL, make_repo

BASE_URL = os.environ.get("APIFIX_BASE_URL", DEFAULT_BASE_URL)
MODEL = os.environ.get("APIFIX_LOCAL_MODEL", "qwen2.5-coder:7b")


def _available() -> str | None:
    """None if the server answers and has the model, else why the tests are skipped."""
    try:
        models = OpenAICompatLLM(MODEL, base_url=BASE_URL, timeout_s=5).list_models()
    except LLMError as exc:
        return f"no local LLM server: {exc}"
    if not any(m == MODEL or m == f"{MODEL}:latest" for m in models):
        return f"{MODEL} is not installed (ollama pull {MODEL}); server has: {models}"
    return None


_why = _available()
pytestmark = [pytest.mark.local_llm, pytest.mark.skipif(_why is not None, reason=_why or "")]


def llm() -> OpenAICompatLLM:
    return OpenAICompatLLM(MODEL, base_url=BASE_URL)  # the CLI's default timeout


def test_local_model_makes_tool_calls():
    """Preflight is a real tool-call round trip; a local endpoint costs nothing."""
    client = llm()
    client.preflight()
    assert client.local and client.cost(Usage(1000, 1000)) == 0.0


def _run_fix(tmp_path, edit_mode: str):
    _docker, why = DockerCLI().available()
    if not _docker:
        pytest.skip(why)
    repo = make_repo(tmp_path / "repo", ORIGINAL)
    result = fix(
        repo,
        "openai-v1",
        llm=llm(),
        sandbox=Sandbox(SandboxConfig(install_timeout_s=600, test_timeout_s=180, memory="1g")),
        config=AgentConfig(
            max_cost_usd=0.0,
            max_retries=2,
            max_tokens_per_job=1_000_000,
            edit_mode=edit_mode,
        ),
    )
    (tmp_path / "trajectory.json").write_text(json.dumps(result.trajectory, indent=2))
    return result


def _report(result, tmp_path, record_property, capsys, **extra):
    summary = {
        "model": MODEL,
        "edit_mode": result.trajectory["job"]["edit_mode"],
        "outcome": result.outcome,
        "verdict": result.verification.verdict if result.verification else None,
        "attempts": result.attempts,
        "total_tokens": result.usage.total,
        "input_tokens": result.usage.input_tokens + result.usage.cache_read_tokens,
        "output_tokens": result.usage.output_tokens,
        "reason": result.reason,
        "trajectory": str(tmp_path / "trajectory.json"),
        **extra,
    }
    for key, value in summary.items():
        record_property(key, value)
    with capsys.disabled():
        print(f"\n[local_llm] {json.dumps(summary)}")

    # Completing cleanly: no crash, a provider-side abort is not acceptable, a trajectory exists.
    assert result.outcome != "aborted" or not result.reason.startswith(
        ("provider_error", "preflight")
    ), result.reason
    assert result.trajectory["attempts"], "no attempt was made"
    if result.outcome == "verified":
        assert "chat.completions" in result.diff


@pytest.mark.docker
def test_local_full_fix_completes(tmp_path, record_property, capsys):
    result = _run_fix(tmp_path, "agent")
    _report(result, tmp_path, record_property, capsys)
    assert any(a["tool_calls"] for a in result.trajectory["attempts"]), "no tool calls"


@pytest.mark.docker
def test_local_whole_file_fix_completes(tmp_path, record_property, capsys):
    result = _run_fix(tmp_path, "whole-file")
    attempts = result.trajectory["attempts"]
    files = [f for a in attempts for f in a["files"]]
    _report(
        result,
        tmp_path,
        record_property,
        capsys,
        files=[(f["path"], [r["parse"] for r in f["replies"]], f["accepted"]) for f in files],
        nudges=[n["kind"] for a in attempts for n in a["nudges"]],
    )
    assert files, "no file was sent"
    assert all(f["replies"] and f["replies"][0]["parse"] for f in files), "no parse outcome"
