"""OpenAI-compatible provider with fake HTTP (scripted transport, and one real local server)."""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
import time
from dataclasses import dataclass, field

import pytest

from apifix.engine.agent_tools import TOOLS as AGENT_TOOLS
from apifix.engine.llm import LLMError, Message, ModelInfo, ToolCall, ToolResult, ToolSpec, Usage
from apifix.engine.llm_openai_compat import (
    MAX_RESPONSE_BYTES,
    TEXT_CALLS_NOTE,
    ConnectFailed,
    HTTPRequest,
    HTTPResponse,
    OpenAICompatLLM,
    RequestTimedOut,
    count_text_tool_calls,
    is_local_host,
    parse_text_tool_call,
    to_openai,
    urllib_transport,
)

TOOLS = [ToolSpec("read_file", "Read a file.", {"type": "object", "properties": {}})]


def reply(content=None, tool_calls=None, finish="stop", usage=True, model="qwen2.5-coder:7b"):
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body = {"model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    if usage:
        body["usage"] = {"prompt_tokens": 120, "completion_tokens": 30}
    return HTTPResponse(200, json.dumps(body).encode())


def tool_call(name="read_file", arguments='{"path": "app.py"}', call_id="call_1"):
    call = {"type": "function", "function": {"name": name, "arguments": arguments}}
    if call_id is not None:
        call["id"] = call_id
    return call


def error(status, message):
    return HTTPResponse(status, json.dumps({"error": {"message": message}}).encode())


@dataclass
class FakeHTTP:
    responses: list = field(default_factory=list)
    requests: list = field(default_factory=list)

    def __call__(self, req):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def body(self, i=-1):
        return json.loads(self.requests[i].body)


def make(*responses, model="qwen2.5-coder:7b", **kw):
    http = FakeHTTP(list(responses))
    llm = OpenAICompatLLM(model, transport=http, sleep=lambda s: None, **kw)
    return llm, http


def complete(llm, messages=None, temperature=0.0):
    return llm.complete(
        system="be careful",
        messages=messages or [Message("user", text="hi")],
        tools=TOOLS,
        max_tokens=1000,
        temperature=temperature,
    )


# --- requests and responses -------------------------------------------------------------------


def test_request_shape(monkeypatch):
    monkeypatch.delenv("APIFIX_BASE_URL", raising=False)
    monkeypatch.delenv("APIFIX_LLM_API_KEY", raising=False)
    llm, http = make(reply("done"))
    complete(llm)
    (req,) = http.requests
    assert (req.method, req.url) == ("POST", "http://localhost:11434/v1/chat/completions")
    assert "Authorization" not in req.headers
    assert req.timeout_s == 3600.0  # local endpoint
    body = http.body()
    assert body["model"] == "qwen2.5-coder:7b"
    assert body["messages"] == [
        {"role": "system", "content": "be careful"},
        {"role": "user", "content": "hi"},
    ]
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert (body["tool_choice"], body["temperature"], body["max_tokens"], body["stream"]) == (
        "auto",
        0.0,
        1000,
        False,
    )


def test_conversation_conversion():
    messages = [
        Message("user", text="task"),
        Message("assistant", text="", tool_calls=[ToolCall("c1", "read_file", {"path": "a"})]),
        Message("user", tool_results=[ToolResult("c1", "x = 1"), ToolResult("c2", "Error", True)]),
        Message("assistant", text="Done."),
        Message("user", text="feedback"),
    ]
    assert to_openai(messages) == [
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "x = 1"},
        {"role": "tool", "tool_call_id": "c2", "content": "Error"},
        {"role": "assistant", "content": "Done."},
        {"role": "user", "content": "feedback"},
    ]


def test_tool_call_response():
    llm, _ = make(reply(None, [tool_call()], finish="tool_calls"))
    r = complete(llm)
    assert r.tool_calls == [ToolCall("call_1", "read_file", {"path": "app.py"})]
    assert (r.stop_reason, r.text, r.model) == ("tool_use", "", "qwen2.5-coder:7b")
    assert r.usage == Usage(input_tokens=120, output_tokens=30)
    assert r.provider_blocks[0]["tool_calls"][0]["function"]["name"] == "read_file"


@pytest.mark.parametrize(
    ("finish", "calls", "stop"),
    [
        ("stop", None, "end_turn"),
        ("length", None, "max_tokens"),
        ("content_filter", None, "refusal"),
        ("stop", [tool_call()], "tool_use"),  # some servers say "stop" with tool calls
        ("tool_calls", [], "end_turn"),
        ("weird", None, "other"),
    ],
)
def test_finish_reasons(finish, calls, stop):
    llm, _ = make(reply("x", calls, finish=finish))
    assert complete(llm).stop_reason == stop


def test_server_quirks():
    calls = [
        tool_call(call_id=None),  # missing id
        tool_call(arguments={"path": "b"}),  # object instead of a JSON string
        tool_call(arguments="{not json"),
        tool_call(arguments='["a"]'),
        tool_call(arguments=""),
    ]
    llm, _ = make(reply(None, calls, finish="tool_calls"))
    got = complete(llm).tool_calls
    assert got[0].id.startswith("call_") and got[0].input == {"path": "app.py"}
    assert got[1].input == {"path": "b"}
    assert "not valid JSON" in got[2].input_error and got[2].input == {}
    assert got[3].input_error == "the arguments must be a JSON object"
    assert got[4].input == {} and not got[4].input_error


def test_content_parts_and_cached_tokens():
    body = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 60},
        },
    }
    llm, _ = make(HTTPResponse(200, json.dumps(body).encode()), model="m")
    r = complete(llm)
    assert r.text == "ab"
    assert r.usage == Usage(input_tokens=40, output_tokens=5, cache_read_tokens=60)


def test_missing_usage_is_estimated_and_noted():
    llm, _ = make(reply("hello", usage=False))
    r = complete(llm)
    assert r.usage.input_tokens > 0 and r.usage.output_tokens == 1
    assert "reported no token usage" in llm.notes[0]


def test_reasoning_is_kept_in_the_trajectory_but_not_replayed():
    body = json.loads(reply("Answer.").body)
    body["choices"][0]["message"]["reasoning_content"] = "thinking..."
    llm, _ = make(HTTPResponse(200, json.dumps(body).encode()))
    r = complete(llm)
    assert r.provider_blocks[0]["reasoning_content"] == "thinking..."
    turn = Message(
        "assistant", text=r.text, provider="openai-compat", provider_blocks=r.provider_blocks
    )
    assert to_openai([turn]) == [{"role": "assistant", "content": "Answer."}]


# --- errors and retries -----------------------------------------------------------------------


def test_model_without_tool_support():
    llm, _ = make(error(400, "registry.ollama.ai/library/gemma:2b does not support tools"))
    with pytest.raises(LLMError, match="does not support tool calls.*qwen2.5-coder:7b"):
        complete(llm)


def test_unknown_model_lists_the_servers_models():
    models = HTTPResponse(
        200, json.dumps({"data": [{"id": "llama3.1:8b"}, {"id": "qwen2.5-coder:14b"}]}).encode()
    )
    llm, http = make(
        error(404, 'model "nope" not found, try pulling it first'), models, model="nope"
    )
    with pytest.raises(LLMError) as exc:
        complete(llm)
    assert "model 'nope' not found" in str(exc.value)
    assert "Models on http://localhost:11434/v1: llama3.1:8b, qwen2.5-coder:14b" in str(exc.value)
    assert "Starting points by hardware" in str(exc.value)
    assert http.requests[1].url == "http://localhost:11434/v1/models"


def test_context_overflow_error():
    llm, _ = make(error(400, "This model's maximum context length is 8192 tokens"))
    with pytest.raises(LLMError, match="exceeds the server's context window"):
        complete(llm)


def test_connection_refused_hints_at_ollama():
    llm, http = make(ConnectFailed("[Errno 111] Connection refused"))
    with pytest.raises(
        LLMError, match=r"no server at http://localhost:11434/v1 \(is Ollama running"
    ):
        complete(llm)
    assert len(http.requests) == 1  # refused: no retries


def test_transient_errors_are_retried():
    llm, http = make(error(503, "loading model"), ConnectFailed("reset by peer"), reply("ok"))
    assert complete(llm).text == "ok"
    assert len(http.requests) == 3


def test_retries_are_bounded():
    llm, http = make(error(503, "busy"), error(503, "busy"), error(503, "still busy"))
    with pytest.raises(LLMError, match="HTTP 503 .*still busy"):
        complete(llm)
    assert len(http.requests) == 3


def test_default_timeout_depends_on_the_endpoint(monkeypatch):
    monkeypatch.delenv("APIFIX_BASE_URL", raising=False)
    assert make()[0].timeout_s == 3600.0
    assert make(base_url="http://192.168.1.20:8000/v1")[0].timeout_s == 3600.0
    assert make(base_url="https://api.example.com/v1")[0].timeout_s == 600.0
    assert make(base_url="https://api.example.com/v1", timeout_s=30)[0].timeout_s == 30.0
    with pytest.raises(LLMError, match="timeout must be positive"):
        make(timeout_s=0)


def test_timeout_is_reported_as_a_timeout_and_not_retried():
    llm, http = make(RequestTimedOut("timed out"), reply("never sent"), timeout_s=1200)
    with pytest.raises(LLMError) as exc:
        complete(llm)
    message = str(exc.value)
    assert "timed out after 1,200 s" in message
    assert "--request-timeout SECONDS" in message
    assert "no server" not in message and "ollama serve" not in message
    assert len(http.requests) == 1


def _opener_raising(monkeypatch, exc):
    def open_(*args, **kwargs):
        raise exc

    monkeypatch.setattr(
        "apifix.engine.llm_openai_compat._OPENER", type("O", (), {"open": open_})()
    )


def test_transport_tells_read_timeouts_from_connect_failures(monkeypatch):
    import urllib.error

    req = HTTPRequest("GET", "http://localhost/v1/models", {}, None, 5)
    # getresponse() / read() raise TimeoutError as is: the server took the request.
    _opener_raising(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(RequestTimedOut):
        urllib_transport(req)
    # Connecting or sending: urllib wraps the error, so the server was not reached.
    _opener_raising(monkeypatch, urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(ConnectFailed):
        urllib_transport(req)
    _opener_raising(monkeypatch, ConnectionResetError("reset by peer"))
    with pytest.raises(ConnectFailed):
        urllib_transport(req)


def test_auth_errors_mention_the_key():
    llm, _ = make(
        error(401, "invalid key"),
        base_url="https://api.example.com/v1",
        models={"qwen2.5-coder:7b": ModelInfo(0, 0, 0, 0)},
    )
    with pytest.raises(LLMError, match="HTTP 401 .*check the API key"):
        complete(llm)


def test_garbage_response():
    llm, _ = make(HTTPResponse(200, b"<html>proxy error</html>"))
    with pytest.raises(LLMError, match="no choices"):
        complete(llm)


# --- preflight ----------------------------------------------------------------------------------


def test_preflight_passes_and_reads_the_ollama_context_window():
    ps = HTTPResponse(
        200,
        json.dumps({"models": [{"name": "qwen2.5-coder:7b", "context_length": 32768}]}).encode(),
    )
    llm, http = make(reply(None, [tool_call("ping", '{"ok": true}')], finish="tool_calls"), ps)
    llm.preflight()
    assert llm.context_window == 32768
    assert http.requests[1].url == "http://localhost:11434/api/ps"
    assert json.loads(http.requests[0].body)["tools"][0]["function"]["name"] == "ping"
    assert "server context window: 32,768 tokens" in llm.notes


def test_preflight_without_ps_endpoint_is_fine():
    llm, _ = make(
        reply(None, [tool_call("ping", "{}")], finish="tool_calls"),
        error(404, "not found"),
        base_url="http://localhost:1234/v1",
    )
    llm.preflight()
    assert llm.context_window is None


def test_preflight_fails_when_no_tool_call_is_made():
    llm, _ = make(reply("Sure! The weather is nice."))
    with pytest.raises(
        LLMError, match="did not make a tool call when asked to. Use a tool-capable"
    ):
        llm.preflight()


def test_preflight_explains_tool_calls_written_as_text_in_strict_mode():
    llm, _ = make(reply('{"name": "ping", "arguments": {"ok": true}}'), strict_tool_calls=True)
    with pytest.raises(
        LLMError,
        match="wrote the call as text instead.*disabled by --strict-tool-calls",
    ):
        llm.preflight()


def test_preflight_passes_for_tool_calls_written_as_text_and_says_so():
    llm, http = make(reply('{"name": "ping", "arguments": {"ok": true}}'), error(404, "no"))
    llm.preflight()
    assert TEXT_CALLS_NOTE in llm.notes
    assert http.requests[-1].url.endswith("/api/ps")


def test_preflight_still_fails_on_prose():
    llm, _ = make(reply("Sure! I would call ping now."))
    with pytest.raises(LLMError, match="did not make a tool call") as exc:
        llm.preflight()
    assert "strict" not in str(exc.value)


def test_small_context_window_fails_before_sending():
    llm, http = make()
    llm.context_window = 2048
    long = [Message("user", text="x" * 20_000)]
    with pytest.raises(
        LLMError,
        match=r"no longer fits the server's context window \(2,048 tokens\).*OLLAMA_CONTEXT_LENGTH",
    ):
        complete(llm, long)
    assert http.requests == []


def test_truncated_first_prompt_is_detected():
    body = json.loads(reply("ok").body)
    body["usage"]["prompt_tokens"] = 2048
    llm, _ = make(HTTPResponse(200, json.dumps(body).encode()), reply("ok"))
    with pytest.raises(LLMError, match="read only 2,048 of ~.* prompt tokens"):
        complete(llm, [Message("user", text="word " * 8000)])


def test_later_cached_prompts_are_not_mistaken_for_truncation():
    small = json.loads(reply("ok").body)
    small["usage"]["prompt_tokens"] = 50  # KV cache: only new tokens evaluated
    llm, _ = make(reply("ok"), HTTPResponse(200, json.dumps(small).encode()))
    complete(llm)
    assert complete(llm, [Message("user", text="word " * 8000)]).text == "ok"


# --- cost, keys, privacy ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("[::1]", True),
        ("192.168.1.20", True),
        ("10.0.0.5", True),
        ("172.16.3.4", True),
        ("gpu-box.local", True),
        ("api.example.com", False),
        ("8.8.8.8", False),
        ("my-server", False),  # no DNS lookups
    ],
)
def test_local_hosts(host, local):
    assert is_local_host(host) is local


def test_local_endpoints_are_free():
    llm, _ = make()
    assert llm.local and llm.cost(Usage(10**9, 10**9)) == 0.0
    assert llm.privacy_notice is None
    assert not any("third-party" in n for n in llm.notes)


def test_hosted_endpoints_need_a_price_and_get_a_notice():
    url = "https://api.example.com/openai/v1"
    llm, _ = make(base_url=url)
    assert llm.cost(Usage(1, 1)) is None
    assert llm.privacy_notice == (
        "notice: repository code will be sent to the third-party server "
        "https://api.example.com/openai/v1"
    )
    assert "repository code is sent to the third-party server" in llm.notes[0]
    priced, _ = make(base_url=url, models={"qwen2.5-coder:7b": ModelInfo(1.0, 2.0, 0, 0)})
    assert priced.cost(Usage(1_000_000, 1_000_000)) == pytest.approx(3.0)


def test_api_key_from_env(monkeypatch):
    monkeypatch.setenv("APIFIX_LLM_API_KEY", "sk-local-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-be-used")
    llm, http = make(
        reply("ok"),
        base_url="https://api.example.com/v1",
        models={"qwen2.5-coder:7b": ModelInfo(0, 0, 0, 0)},
    )
    complete(llm)
    assert http.requests[0].headers["Authorization"] == "Bearer sk-local-secret"
    monkeypatch.setenv("MY_KEY", "sk-other")
    llm, http = make(reply("ok"), api_key_env="MY_KEY")
    complete(llm)
    assert http.requests[0].headers["Authorization"] == "Bearer sk-other"


def test_openai_api_key_is_never_read(monkeypatch):
    monkeypatch.delenv("APIFIX_LLM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-be-used")
    llm, http = make(reply("ok"))
    complete(llm)
    assert "Authorization" not in http.requests[0].headers


def test_key_is_never_sent_over_remote_http(monkeypatch):
    monkeypatch.setenv("APIFIX_LLM_API_KEY", "sk-secret")
    with pytest.raises(LLMError, match="over plain http to api.example.com") as exc:
        make(base_url="http://api.example.com/v1")
    assert "sk-secret" not in str(exc.value)
    make(base_url="http://192.168.1.5:8000/v1")  # local http is fine


def test_key_never_appears_in_errors_or_notes(monkeypatch):
    monkeypatch.setenv("APIFIX_LLM_API_KEY", "sk-secret-123")
    llm, _ = make(error(401, "bad"), base_url="https://api.example.com/v1")
    with pytest.raises(LLMError) as exc:
        complete(llm)
    assert "sk-secret-123" not in str(exc.value) + json.dumps(llm.notes) + llm.endpoint


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://x/v1", "invalid base URL"),
        ("localhost:11434", "invalid base URL"),
        ("https://user:pw@api.example.com/v1", "not in the base URL"),
    ],
)
def test_bad_base_urls(url, message):
    with pytest.raises(LLMError, match=message):
        make(base_url=url)


def test_model_is_required_and_suggestions_are_given():
    models = HTTPResponse(200, json.dumps({"data": [{"id": "qwen2.5-coder:7b"}]}).encode())
    with pytest.raises(LLMError) as exc:
        make(models, model=None)
    text = str(exc.value)
    assert text.startswith("--model is required for --provider openai-compat.")
    assert "Models on http://localhost:11434/v1: qwen2.5-coder:7b." in text
    assert "24 GB GPU or 32-48 GB Mac: qwen2.5-coder:32b" in text


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("APIFIX_BASE_URL", "http://localhost:1234/v1/")
    llm, http = make(reply("ok"))
    complete(llm)
    assert http.requests[0].url == "http://localhost:1234/v1/chat/completions"


# --- the real urllib path, against a local HTTP server ------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.seen.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        if self.path == "/v1/redirect/chat/completions":
            self.send_response(302)
            self.send_header("Location", "http://example.com/steal")
            self.end_headers()
            return
        if self.path == "/v1/slow/chat/completions":
            time.sleep(0.5)
        data = reply(None, [tool_call()], finish="tool_calls").body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):  # the client timed out (slow path)
            self.wfile.write(data)


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    _Handler.seen = []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_real_http_round_trip(server, monkeypatch):
    monkeypatch.setenv("APIFIX_LLM_API_KEY", "sk-local")
    llm = OpenAICompatLLM("m", base_url=f"{server}/v1")
    r = complete(llm)
    assert r.tool_calls[0].input == {"path": "app.py"}
    (path, auth, body) = _Handler.seen[0]
    assert (path, auth, body["model"]) == ("/v1/chat/completions", "Bearer sk-local", "m")


def test_redirects_are_not_followed(server):
    llm = OpenAICompatLLM("m", base_url=f"{server}/v1/redirect")
    with pytest.raises(LLMError, match="HTTP 302"):
        complete(llm)
    assert len(_Handler.seen) == 1


def test_real_slow_server_times_out(server):
    llm = OpenAICompatLLM("m", base_url=f"{server}/v1/slow", timeout_s=0.1)
    with pytest.raises(LLMError, match=r"timed out after 0.1 s .*--request-timeout"):
        complete(llm)
    assert len(_Handler.seen) == 1


def test_real_connection_refused():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    llm = OpenAICompatLLM("m", base_url=f"http://127.0.0.1:{port}/v1")
    with pytest.raises(LLMError, match="no server at .*is Ollama running"):
        complete(llm)


def test_oversized_responses_are_refused(monkeypatch):
    class Big:
        status = 200

        def read(self, n):
            return b"x" * n

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(
        "apifix.engine.llm_openai_compat._OPENER", type("O", (), {"open": lambda *a, **k: Big()})()
    )
    with pytest.raises(LLMError, match=f"larger than {MAX_RESPONSE_BYTES:,} bytes"):
        urllib_transport(HTTPRequest("GET", "http://localhost/v1/models", {}, None, 5))


def test_docs_and_error_suggest_the_same_models():
    import re
    from pathlib import Path

    from apifix.engine.llm_openai_compat import MODEL_SUGGESTIONS

    root = Path(__file__).parent.parent
    suggested = re.findall(r"(?:qwen2\.5-coder|llama3\.3):\d+b", MODEL_SUGGESTIONS)
    assert len(suggested) == 4
    for doc in ("README.md", "CLAUDE.md"):
        text = (root / doc).read_text()
        assert all(f"`{m}`" in text for m in suggested), doc


# --- tool calls written as text ---------------------------------------------------------------

READ = '{"name": "read_file", "arguments": {"path": "app.py"}}'
EDIT = (
    '{"name": "edit_file", "arguments": {"path": "app.py", "old_string": "a", "new_string": "b"}}'
)


def text_call(text, **kw):
    llm, _ = make(reply(text), **kw)
    return llm, llm.complete(
        system="s",
        messages=[Message("user", text="go")],
        tools=AGENT_TOOLS,
        max_tokens=100,
        temperature=None,
    )


@pytest.mark.parametrize(
    "text",
    [
        READ,
        f"\n  {READ}  \n",
        f"```json\n{READ}\n```",
        f"```\n{READ}\n```",
        f"<tool_call>\n{READ}\n</tool_call>",
        READ.replace('{"path": "app.py"}', '"{\\"path\\": \\"app.py\\"}"'),  # string arguments
    ],
    ids=["bare", "whitespace", "json-fence", "plain-fence", "tool-call-tags", "string-arguments"],
)
def test_text_tool_call_is_parsed(text):
    llm, r = text_call(text)
    (c,) = r.tool_calls
    assert (c.name, c.input, c.source) == ("read_file", {"path": "app.py"}, "parsed_from_text")
    assert c.arguments_as_string == ("\\" in text)
    assert c.id.startswith("call_") and c.input_error == ""
    assert (r.stop_reason, r.text, r.multiple_text_tool_calls) == ("tool_use", "", 0)
    assert TEXT_CALLS_NOTE in llm.notes
    # replayed as a structured assistant tool call, not as text
    (msg,) = to_openai([Message("assistant", tool_calls=r.tool_calls)])
    assert msg["content"] == "" and msg["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "app.py"}


@pytest.mark.parametrize(
    "text",
    [
        f"I will read the file first. {READ}",
        f"{READ}\nThen I will edit it.",
        f"Here you go:\n```json\n{READ}\n```",
        READ.replace("read_file", "write_file"),  # not an offered tool
        READ.replace("read_file", "Read_File"),
        READ.replace('"app.py"', "42"),  # wrong type
        READ.replace('"path"', '"file"'),  # unknown + missing required argument
        '{"name": "read_file", "arguments": {}}',  # missing required argument
        '{"name": "read_file", "arguments": {"path": null}}',
        READ.replace('"app.py"}', '"app.py", "mode": "r"}'),  # unknown argument
        READ.replace('"arguments"', '"parameters"'),
        READ[:-1] + ', "id": "x"}',  # extra top-level key
        '{"name": "read_file", "arguments": "path=app.py"}',  # string that is not JSON
        '{"name": "read_file", "arguments": "[{\\"path\\": \\"a\\"}]"}',  # not one object
        '{"name": "read_file", "arguments": "{\\"path\\": \\"a\\"} {}"}',  # two objects
        '{"name": "read_file", "name": "edit_file", "arguments": {"path": "a"}}',  # duplicate
        f"[{READ}]",
        f"<tool_call>```json\n{READ}\n```</tool_call>",  # nested wrappers
        f"```json\n{READ}\n",  # unclosed fence
        f"```python\n{READ}\n```",
        f"{READ}}}",
        "",
    ],
)
def test_text_that_is_not_exactly_one_valid_call_stays_text(text):
    _, r = text_call(text)
    assert r.tool_calls == [] and r.text == text
    assert r.stop_reason == "end_turn" and r.multiple_text_tool_calls == 0


@pytest.mark.parametrize(
    ("text", "count"),
    [
        (f"{READ}\n{EDIT}", 2),
        (f"{READ}{READ}{READ}", 3),
        (f"<tool_call>\n{READ}\n</tool_call>\n<tool_call>\n{EDIT}\n</tool_call>", 2),
        (f"```json\n{READ}\n```\n```json\n{READ}\n```", 2),
        (f"{READ}\n" + '{"name": "read_file", "arguments": {"path": 1}}', 2),  # not validated
    ],
)
def test_several_text_calls_are_counted_not_run(text, count):
    _, r = text_call(text)
    assert r.tool_calls == [] and r.text == text and r.stop_reason == "end_turn"
    assert r.multiple_text_tool_calls == count


@pytest.mark.parametrize(
    "text",
    [
        f"First {READ} then {EDIT}",
        f"{READ}\n" + '{"name": "nope", "arguments": {}}',
        f"{READ}\n[1, 2]",
        f"[{READ}, {EDIT}]",
    ],
)
def test_several_objects_that_are_not_all_calls_are_plain_text(text):
    assert count_text_tool_calls(text, AGENT_TOOLS) == 0


def test_strict_mode_never_parses_text():
    llm, r = text_call(READ, strict_tool_calls=True)
    assert (r.tool_calls, r.text, r.stop_reason) == ([], READ, "end_turn")
    _, r = text_call(f"{READ}\n{READ}", strict_tool_calls=True)
    assert r.multiple_text_tool_calls == 0
    assert TEXT_CALLS_NOTE not in llm.notes


def test_structured_calls_win_over_text():
    llm, _ = make(
        reply(READ.replace("app.py", "other.py"), tool_calls=[tool_call()], finish="tool_calls")
    )
    r = complete(llm)
    (c,) = r.tool_calls
    assert (c.source, c.input) == ("structured", {"path": "app.py"})
    assert r.text != "" and TEXT_CALLS_NOTE not in llm.notes


def test_refusals_are_not_parsed():
    llm, _ = make(reply(READ, finish="content_filter"))
    r = llm.complete(
        system="s",
        messages=[Message("user", text="go")],
        tools=AGENT_TOOLS,
        max_tokens=9,
        temperature=None,
    )
    assert (r.stop_reason, r.tool_calls) == ("refusal", [])


def test_parser_functions_directly():
    assert parse_text_tool_call(READ, []) is None  # nothing offered
    assert parse_text_tool_call(READ, AGENT_TOOLS).name == "read_file"
    assert count_text_tool_calls(READ, AGENT_TOOLS) == 0  # one call is not "several"


# --- whole-file mode: no tools ----------------------------------------------------------------


def test_no_tools_or_tool_choice_without_tools():
    llm, http = make(reply("```python\nx = 1\n```"))
    r = llm.complete(
        system="s",
        messages=[Message("user", text="go")],
        tools=[],
        max_tokens=50,
        temperature=None,
    )
    body = http.body()
    assert "tools" not in body and "tool_choice" not in body
    assert (r.text, r.tool_calls, r.stop_reason) == ("```python\nx = 1\n```", [], "end_turn")


def test_preflight_without_tools_sends_a_plain_request():
    llm, http = make(
        reply("OK"),
        HTTPResponse(
            200,
            json.dumps({"models": [{"name": "qwen2.5-coder:7b", "context_length": 8192}]}).encode(),
        ),
    )
    llm.preflight(require_tools=False)  # a plain reply is fine: no tool call needed
    body = http.body(0)
    assert "tools" not in body and body["max_tokens"] == 5
    assert body["messages"][-1]["content"] == "Reply with OK."
    assert llm.context_window == 8192
    assert TEXT_CALLS_NOTE not in llm.notes


def test_preflight_without_tools_still_reports_server_errors():
    llm, _ = make(error(404, "model 'nope' not found"), error(404, "x"))
    with pytest.raises(LLMError, match="not found"):
        llm.preflight(require_tools=False)
