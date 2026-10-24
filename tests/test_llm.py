"""Provider-neutral LLM layer and the Claude provider (fake SDK client, no network)."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from anthropic.types import Message as SDKMessage

from apifix.engine import llm_anthropic
from apifix.engine.llm import (
    ANTHROPIC_MODELS,
    LLMError,
    Message,
    ModelInfo,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    get_llm,
    load_model_table,
)
from apifix.engine.llm_anthropic import FALLBACK_BETA, AnthropicLLM, to_anthropic

# --- model table ------------------------------------------------------------------------------


def test_cost():
    info = ModelInfo(input=4.0, output=20.0, cache_read=0.2, cache_write=5.0)
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=2_000_000)
    assert info.cost(usage) == pytest.approx(4.0 + 2.0 + 0.4)
    assert usage.total == 3_100_000
    assert (usage + usage).output_tokens == 200_000


def test_default_table_has_the_default_model():
    assert ANTHROPIC_MODELS["claude-opus-5-5"].input == 4.0
    assert not ANTHROPIC_MODELS["claude-opus-5-5"].temperature
    assert ANTHROPIC_MODELS["claude-haiku-4-5"].temperature


def test_price_overrides_from_toml(tmp_path, monkeypatch):
    prices = tmp_path / "prices.toml"
    prices.write_text(
        '[models."claude-opus-5-5"]\ninput = 3.5\n\n'
        '[models."claude-new-6"]\ninput = 1\noutput = 2\ncache_read = 0.1\ncache_write = 1.25\n'
        "temperature = true\n"
    )
    table = load_model_table(ANTHROPIC_MODELS, prices)
    assert table["claude-opus-5-5"].input == 3.5
    assert table["claude-opus-5-5"].output == 20.0  # untouched fields kept
    assert table["claude-new-6"] == ModelInfo(1, 2, 0.1, 1.25, temperature=True)
    assert ANTHROPIC_MODELS["claude-opus-5-5"].input == 4.0  # defaults not mutated

    monkeypatch.setenv("APIFIX_PRICES", str(prices))
    assert load_model_table(ANTHROPIC_MODELS)["claude-opus-5-5"].input == 3.5


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ('[models."claude-x"]\ninput = 1\n', "needs"),
        ('[models."claude-opus-5-5"]\nprice = 1\n', "allowed keys"),
        ('[models."claude-opus-5-5"]\ninput = "cheap"\n', "wrong type"),
        ('[models."claude-opus-5-5"]\ntemperature = 1\n', "wrong type"),
        ("[models\n", "cannot read"),
    ],
)
def test_bad_price_files(tmp_path, toml, message):
    path = tmp_path / "p.toml"
    path.write_text(toml)
    with pytest.raises(ValueError, match=message):
        load_model_table(ANTHROPIC_MODELS, path)


def test_get_llm(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("APIFIX_MODEL", raising=False)
    assert get_llm().model == "claude-opus-5-5"
    monkeypatch.setenv("APIFIX_MODEL", "claude-sonnet-5-5")
    assert get_llm("anthropic").model == "claude-sonnet-5-5"
    with pytest.raises(ValueError, match="unknown provider 'openai'"):
        get_llm("openai")


# --- Claude provider ----------------------------------------------------------------------------


def sdk_message(content, stop_reason="end_turn", model="claude-opus-5-5", **extra):
    return SDKMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
            },
            **extra,
        }
    )


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.messages = SimpleNamespace(create=lambda **kw: self._create("messages", kw))
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: self._create("beta", kw))
        )

    def _create(self, endpoint, kwargs):
        self.calls.append((endpoint, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


TOOLS = [ToolSpec("read_file", "Read a file.", {"type": "object", "properties": {}})]


def complete(llm, messages=None, temperature=0.0):
    return llm.complete(
        system="be careful",
        messages=messages or [Message("user", text="hi")],
        tools=TOOLS,
        max_tokens=1000,
        temperature=temperature,
    )


def test_request_for_the_default_model():
    client = FakeClient(sdk_message([{"type": "text", "text": "done"}]))
    llm = AnthropicLLM("claude-opus-5-5", client=client)
    complete(llm)
    ((endpoint, kw),) = client.calls
    assert endpoint == "messages"  # fallback is off by default
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"] == [{"type": "text", "text": "be careful"}]
    assert kw["tools"] == [
        {"name": "read_file", "description": "Read a file.", "input_schema": TOOLS[0].input_schema}
    ]
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert kw["cache_control"] == {"type": "ephemeral"}
    assert kw["output_config"] == {"effort": "high"}
    assert "temperature" not in kw and "extra_body" not in kw and "betas" not in kw
    assert llm.notes == ["temperature=0.0 not sent: claude-opus-5-5 does not accept it"]


def test_temperature_is_sent_to_models_that_accept_it():
    client = FakeClient(sdk_message([{"type": "text", "text": "ok"}], model="claude-haiku-4-5"))
    llm = AnthropicLLM("claude-haiku-4-5", client=client)
    complete(llm)
    kw = client.calls[0][1]
    assert kw["extra_body"] == {"temperature": 0.0}
    assert "output_config" not in kw  # haiku 4.5 does not take effort
    assert llm.notes == []


def test_unknown_model_has_no_price():
    llm = AnthropicLLM("claude-future-9", client=FakeClient())
    assert llm.cost(Usage(1, 1)) is None
    assert "not in the model table" in llm.notes[0]
    assert "output_config" not in llm.request(
        system="s", messages=[], tools=[], max_tokens=1, temperature=None
    )


def test_response_conversion_and_cost():
    client = FakeClient(
        sdk_message(
            [
                {"type": "thinking", "thinking": "", "signature": "sig-1"},
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a"}},
            ],
            stop_reason="tool_use",
        )
    )
    llm = AnthropicLLM("claude-opus-5-5", client=client)
    r = complete(llm)
    assert (r.text, r.stop_reason, r.model) == ("Reading.", "tool_use", "claude-opus-5-5")
    assert r.tool_calls == [ToolCall("tu_1", "read_file", {"path": "a"})]
    assert r.usage == Usage(10, 5, 100, 20)
    assert llm.cost(r.usage) == pytest.approx((10 * 4 + 5 * 20 + 100 * 0.2 + 20 * 5) / 1e6)
    assert r.provider_blocks[0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}


def test_assistant_turns_are_replayed_verbatim():
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "sig-1"},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a"}},
    ]
    turn = Message(
        "assistant",
        tool_calls=[ToolCall("tu_1", "read_file", {"path": "a"})],
        provider="anthropic",
        provider_blocks=blocks,
    )
    assert to_anthropic(turn) == {"role": "assistant", "content": blocks}
    # another provider's turn is rebuilt from the neutral fields
    other = Message("assistant", text="hi", tool_calls=turn.tool_calls, provider="fake")
    assert to_anthropic(other)["content"] == [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a"}},
    ]


def test_tool_results():
    m = Message(
        "user",
        text="note",
        tool_results=[ToolResult("tu_1", "content"), ToolResult("tu_2", "Error: x", True)],
    )
    assert to_anthropic(m) == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "content"},
            {"type": "tool_result", "tool_use_id": "tu_2", "content": "Error: x", "is_error": True},
            {"type": "text", "text": "note"},
        ],
    }


def test_refusal_carries_its_category():
    client = FakeClient(
        sdk_message(
            [],
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "cyber", "explanation": "declined"},
        )
    )
    r = complete(AnthropicLLM("claude-opus-5-5", client=client))
    assert (r.stop_reason, r.refusal) == ("refusal", "cyber: declined")


def test_fallback_when_enabled_records_the_answering_model():
    client = FakeClient(sdk_message([{"type": "text", "text": "ok"}], model="claude-opus-4-8"))
    llm = AnthropicLLM("claude-opus-5-5", client=client, fallback=True)
    r = complete(llm)
    endpoint, kw = client.calls[0]
    assert endpoint == "beta"
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert r.model == "claude-opus-4-8"


def test_sdk_errors_become_llm_errors():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient(anthropic.APIConnectionError(request=request))
    with pytest.raises(LLMError, match="APIConnectionError: Connection error."):
        complete(AnthropicLLM("claude-opus-5-5", client=client))


def test_a_connection_error_mid_job_is_waited_out():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    ok = sdk_message([{"type": "text", "text": "done"}])
    lost = anthropic.APIConnectionError(request=request)
    waits: list[float] = []
    client = FakeClient(ok, lost, lost, ok, lost, lost, lost, lost)
    llm = AnthropicLLM("claude-opus-5-5", client=client, sleep=waits.append)
    complete(llm)
    assert complete(llm).text == "done"
    assert waits == [30.0, 60.0]
    assert llm.notes[-1].startswith("endpoint unreachable mid-job 2 time(s); apifix waited 90 s")
    with pytest.raises(LLMError, match=r"apifix waited 30, 60, 120 s and tried again\)$"):
        complete(llm)


def test_a_timeout_mid_job_is_not_waited_out():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    ok = sdk_message([{"type": "text", "text": "done"}])
    waits: list[float] = []
    client = FakeClient(ok, anthropic.APITimeoutError(request=request))
    llm = AnthropicLLM("claude-opus-5-5", client=client, sleep=waits.append)
    complete(llm)
    with pytest.raises(LLMError, match="APITimeoutError"):
        complete(llm)
    assert waits == []


def test_api_key_comes_from_the_host_environment_only(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicLLM("claude-opus-5-5")
    seen = {}
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: seen.update(kw) or object())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    AnthropicLLM("claude-opus-5-5")
    assert seen["api_key"] == "sk-from-env"
    assert llm_anthropic.API_KEY_ENV == "ANTHROPIC_API_KEY"


def test_no_tools_key_when_no_tools_are_offered():
    llm = AnthropicLLM("claude-opus-5-5", client=FakeClient())
    kw = llm.request(
        system="s", messages=[Message("user", text="hi")], tools=[], max_tokens=9, temperature=None
    )
    assert "tools" not in kw
    kw = llm.request(
        system="s",
        messages=[Message("user", text="hi")],
        tools=TOOLS,
        max_tokens=9,
        temperature=None,
    )
    assert [t["name"] for t in kw["tools"]] == ["read_file"]
