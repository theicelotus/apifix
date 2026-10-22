"""Any OpenAI-compatible chat-completions endpoint with tool calling: Ollama, LM Studio, vLLM,
hosted free tiers. Plain stdlib HTTP, no SDK.

- Local endpoints (loopback or private-network hosts) cost $0. Others are priced from the model
  table (TOML); token and tool-call limits apply either way.
- An optional API key comes from the host environment (``APIFIX_LLM_API_KEY`` or a variable
  named with ``api_key_env``). It is never sent over plain http to a remote host and never
  appears in errors, notes or the trajectory (server error messages are scrubbed of it).
- Rate limits (HTTP 429, e.g. hosted free tiers) are waited out inside the provider, as long as
  the server says (``Retry-After``, ``retry-after-ms`` or "try again in 7.5s") or with
  exponential backoff, so the agent sees a slow request rather than a failure. Daily / quota
  limits and waits longer than ``RATE_LIMIT_MAX_WAIT_S`` stop the job with a clear error.
- ``preflight()`` checks that the model really makes tool calls before the job spends time on a
  sandbox install, and (for Ollama) reads the runtime context window so a too-small one fails
  loudly instead of silently truncating the prompt.
- Some models write a tool call as their reply text instead of in the tool-call field (e.g.
  qwen2.5-coder:7b leaves out the ``<tool_call>`` tags Ollama parses). A reply that is exactly
  one well-formed call to an offered tool, optionally in one ```json fence or ``<tool_call>``
  block, with valid arguments, is used as that call (``source="parsed_from_text"``); anything
  else stays text. ``strict_tool_calls=True`` turns this off.
"""

from __future__ import annotations

import email.utils
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from apifix import __version__
from apifix.engine.llm import (
    LLMError,
    LLMResponse,
    Message,
    ModelInfo,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
    validate_tool_input,
)

DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama
BASE_URL_ENV = "APIFIX_BASE_URL"
API_KEY_ENV = "APIFIX_LLM_API_KEY"
# Sent on every request: some hosted endpoints' firewalls block urllib's default
# "Python-urllib/3.x" with a bare 403.
USER_AGENT = f"apifix/{__version__}"
MAX_RESPONSE_BYTES = 20_000_000
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS_S = (1.0, 4.0)
# HTTP 429: waits per request when the server gives no hint; the longest single wait accepted.
RATE_LIMIT_BACKOFF_S = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
RATE_LIMIT_MAX_WAIT_S = 120.0
RATE_LIMIT_NOTE = "rate limited by the server"
# A 429 for a daily or account quota: waiting minutes would not help.
_DAILY_LIMIT = re.compile(
    r"per day|\b(?:RPD|TPD)\b|daily|insufficient_quota|exceeded your current quota", re.I
)
_TRY_AGAIN = re.compile(r"try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.I)
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_DURATION_UNIT_S = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
# Seconds to wait for one reply. Local models on CPU can need far longer than hosted ones for a
# whole file (prompt evaluation plus several thousand output tokens at a few tokens a second).
LOCAL_TIMEOUT_S = 3600.0
HOSTED_TIMEOUT_S = 600.0
CHARS_PER_TOKEN = 4  # for truncation checks: an underestimate of tokens is the safe side
CONTEXT_HEADROOM_TOKENS = 1024
TRUNCATION_RATIO = 0.7  # room for the reply when checking against the context window

# Starting points by hardware (4-bit quantised, as Ollama pulls by default).
MODEL_SUGGESTIONS = (
    "8 GB GPU or 16 GB RAM: qwen2.5-coder:7b (tiny repos only; use --edit-mode whole-file); "
    "12-16 GB GPU or 32 GB RAM / Apple Silicon: qwen2.5-coder:14b; "
    "24 GB GPU or 32-48 GB Mac: qwen2.5-coder:32b (smallest for real repos); "
    "48 GB+ GPU or 64 GB+ Mac: llama3.3:70b"
)
_FINISH: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}
TEXT_CALLS_NOTE = (
    "the model writes tool calls as text; apifix parses replies that are exactly one JSON "
    "tool call (--strict-tool-calls disables this)"
)
_PING = ToolSpec(
    "ping",
    "Reply to a connectivity check.",
    {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class HTTPRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout_s: float


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)  # names lower-cased


class ConnectFailed(Exception):
    """The server could not be reached (refused, DNS, reset before a response)."""


class RequestTimedOut(Exception):
    """The server accepted the request but sent no complete reply within the timeout."""


Transport = Callable[[HTTPRequest], HTTPResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surfaces as an HTTPError with the 3xx status


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(req: HTTPRequest) -> HTTPResponse:
    request = urllib.request.Request(
        req.url, data=req.body, headers=dict(req.headers), method=req.method
    )
    try:
        with _OPENER.open(request, timeout=req.timeout_s) as resp:
            return HTTPResponse(resp.status, _read_bounded(resp), _lower_keys(resp.headers))
    except urllib.error.HTTPError as exc:
        return HTTPResponse(exc.code, _read_bounded(exc), _lower_keys(exc.headers))
    except urllib.error.URLError as exc:  # raised while connecting / sending (incl. timeouts)
        raise ConnectFailed(str(exc.reason)) from exc
    except TimeoutError as exc:  # waiting for or reading the reply
        raise RequestTimedOut(str(exc) or "timed out") from exc
    except ConnectionError as exc:
        raise ConnectFailed(str(exc) or type(exc).__name__) from exc


def _lower_keys(headers: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _read_bounded(stream: Any) -> bytes:
    data = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise LLMError(f"response larger than {MAX_RESPONSE_BYTES:,} bytes")
    return data


def is_local_host(host: str | None) -> bool:
    """Loopback or private-network host (no DNS lookups: names other than localhost / *.local
    count as remote)."""
    if not host:
        return False
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _redact(url: str) -> str:
    """The URL without userinfo, query or fragment (for notes and the trajectory)."""
    parts = urlsplit(url)
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


class OpenAICompatLLM:
    provider = "openai-compat"

    def __init__(
        self,
        model: str | None,
        *,
        base_url: str | None = None,
        api_key_env: str = API_KEY_ENV,
        models: Mapping[str, ModelInfo] | None = None,
        timeout_s: float | None = None,
        strict_tool_calls: bool = False,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        url = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise LLMError(f"invalid base URL {url!r}: expected http(s)://host[:port]/path")
        if parts.username or parts.password:
            raise LLMError("put credentials in an environment variable, not in the base URL")
        self.base_url = url.rstrip("/")
        self.endpoint = _redact(self.base_url)
        self.local = is_local_host(parts.hostname)
        self.models = dict(models or {})
        if timeout_s is None:
            timeout_s = LOCAL_TIMEOUT_S if self.local else HOSTED_TIMEOUT_S
        if timeout_s <= 0:
            raise LLMError("the request timeout must be positive")
        self.timeout_s = float(timeout_s)
        self.strict_tool_calls = strict_tool_calls
        self.text_tool_calls_seen = False  # a call was parsed from reply text (never if strict)
        self.transport = transport
        self.sleep = sleep
        self.notes: list[str] = []
        self.context_window: int | None = None
        self._requests = 0
        self._usage_estimated = False
        self.rate_limit_waits = 0
        self.rate_limit_wait_s = 0.0

        self._key = os.environ.get(api_key_env) or None
        if self._key and parts.scheme == "http" and not self.local:
            raise LLMError(
                f"refusing to send the API key from ${api_key_env} over plain http to "
                f"{parts.hostname}; use https"
            )
        if not self.local:
            self.notes.append(f"repository code is sent to the third-party server {self.endpoint}")
        if not model:
            raise LLMError(self._model_help("--model is required for --provider openai-compat"))
        self.model = model

    # --- facts ------------------------------------------------------------------------------

    @property
    def privacy_notice(self) -> str | None:
        if self.local:
            return None
        return f"notice: repository code will be sent to the third-party server {self.endpoint}"

    def cost(self, usage: Usage, model: str | None = None) -> float | None:
        if self.local:
            return 0.0
        info = self.models.get(model or self.model) or self.models.get(self.model)
        return info.cost(usage) if info else None

    def list_models(self) -> list[str]:
        status, data = self._call("GET", "/models", None, retry=False)
        if status != 200 or not isinstance(data, dict):
            return []
        return sorted(
            str(m["id"]) for m in data.get("data", []) if isinstance(m, dict) and "id" in m
        )

    def _model_help(self, problem: str) -> str:
        try:
            available = self.list_models()
        except LLMError:
            available = []
        listed = f" Models on {self.endpoint}: {', '.join(available)}." if available else ""
        return f"{problem}.{listed} Starting points by hardware: {MODEL_SUGGESTIONS}."

    # --- HTTP -------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    def _call(
        self, method: str, path: str, body: dict | None, *, retry: bool = True, root: bool = False
    ) -> tuple[int, Any]:
        base = self.base_url.removesuffix("/v1") if root else self.base_url
        req = HTTPRequest(
            method,
            base + path,
            self._headers(),
            json.dumps(body).encode() if body is not None else None,
            self.timeout_s,
        )
        delays = list(RETRY_DELAYS_S) if retry else []
        backoff = list(RATE_LIMIT_BACKOFF_S) if retry else []
        while True:
            try:
                resp = self.transport(req)
            except ConnectFailed as exc:
                if delays and "refused" not in str(exc).lower():
                    self.sleep(delays.pop(0))
                    continue
                hint = " (is Ollama running? `ollama serve`)" if self.local else ""
                raise LLMError(
                    f"no server at {self.endpoint}{hint}: {self._scrub(str(exc))}"
                ) from None
            except RequestTimedOut:
                # Not retried: the server is up but slow, and a retry would wait just as long.
                raise LLMError(
                    f"request to {self.endpoint} timed out after {self.timeout_s:,g} s "
                    "without a complete reply (the server is reachable, but large files on a "
                    "slow or CPU-only machine can take longer). Raise the limit with "
                    "--request-timeout SECONDS"
                ) from None
            try:
                data = json.loads(resp.body.decode("utf-8")) if resp.body else None
            except (UnicodeDecodeError, ValueError):
                data = None
            if resp.status == 429 and retry:
                self._wait_for_rate_limit(resp, data, backoff)
                continue
            if resp.status in RETRY_STATUSES and delays:
                self.sleep(delays.pop(0))
                continue
            return resp.status, data

    def _wait_for_rate_limit(self, resp: HTTPResponse, data: Any, backoff: list[float]) -> None:
        """Sleep as long as the server asks (or the next backoff step), or raise when waiting
        cannot help: a daily / quota limit, a wait over RATE_LIMIT_MAX_WAIT_S, or no retries
        left. Waits are not failures: the agent only sees the eventual reply."""
        message = self._server_message(data)
        asked = server_wait_s(resp.headers, message)
        shown = f" ({message})" if message else ""
        after = f"; the server says to try again in {_format_wait(asked)}" if asked else ""
        if _DAILY_LIMIT.search(message):
            raise LLMError(
                f"daily or quota limit reached on {self.endpoint}{shown}{after}. apifix "
                "stopped instead of waiting. Try again after the limit resets, or use another "
                "model (free-tier limits are often per model) or provider"
            )
        if asked is not None and asked > RATE_LIMIT_MAX_WAIT_S:
            raise LLMError(
                f"rate limited by {self.endpoint}{shown}{after}, longer than apifix waits "
                f"({RATE_LIMIT_MAX_WAIT_S:,.0f} s). Try again later, or use another model or "
                "provider"
            )
        if not backoff:
            raise LLMError(
                f"rate limited by {self.endpoint} {len(RATE_LIMIT_BACKOFF_S) + 1} times in a "
                f"row for one request{shown}. Try again later, or use another model or provider"
            )
        step = backoff.pop(0)
        wait = max(asked, 1.0) if asked is not None else step
        self.sleep(wait)
        self.rate_limit_waits += 1
        self.rate_limit_wait_s += wait
        self.notes[:] = [n for n in self.notes if not n.startswith(RATE_LIMIT_NOTE)]
        self.notes.append(
            f"{RATE_LIMIT_NOTE} {self.rate_limit_waits} time(s); apifix waited "
            f"{self.rate_limit_wait_s:,.0f} s in total and retried (request durations include "
            "the waits)"
        )

    def _scrub(self, text: str) -> str:
        """`text` without the API key (some servers echo a rejected key in their message)."""
        return text.replace(self._key, "[API key]") if self._key else text

    def _server_message(self, data: Any) -> str:
        message = ""
        if isinstance(data, dict):
            err = data.get("error", data.get("message", ""))
            message = err.get("message", "") if isinstance(err, dict) else str(err)
        return self._scrub(message[:500])

    def _error(self, status: int, data: Any, *, max_tokens: int | None = None) -> LLMError:
        message = self._server_message(data) or "(no error message)"
        low = message.lower()
        if status == 413 or "request too large" in low:
            reserved = (
                f" This request allows for up to {max_tokens:,} reply tokens, which some "
                "servers count as well."
                if max_tokens
                else ""
            )
            return LLMError(
                f"HTTP {status} from {self.endpoint}: {message}. One request is larger than "
                f"the server accepts (on free tiers, usually the per-minute token limit).{reserved}"
                " Lower --max-output-tokens or --max-file-bytes, or use a model with a "
                "higher limit"
            )
        if "does not support tools" in low or ("tool" in low and "not support" in low):
            return LLMError(
                f"model {self.model} does not support tool calls ({message}). Use a "
                f"tool-capable model. {MODEL_SUGGESTIONS}"
            )
        if "context length" in low or "context window" in low or "too many tokens" in low:
            return LLMError(f"the prompt exceeds the server's context window: {message}")
        if status == 404 and ("model" in low or "not found" in low):
            return LLMError(self._model_help(f"model {self.model!r} not found ({message})"))
        if status == 403 and not self.local and not isinstance(data, dict):
            return LLMError(
                f"HTTP 403 from {self.endpoint} with an empty or non-JSON reply: the request was "
                "probably blocked before it reached the API (a firewall, a region restriction "
                "or a blocked client), not refused because of the API key. Check that this "
                "machine can reach the endpoint"
            )
        if status in (401, 403):
            return LLMError(f"HTTP {status} from {self.endpoint}: {message} (check the API key)")
        return LLMError(f"HTTP {status} from {self.endpoint}: {message}")

    # --- requests ---------------------------------------------------------------------------

    def request_body(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + to_openai(messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:  # whole-file mode offers none; some servers reject tool_choice without tools
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            body["tool_choice"] = "auto"
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        temperature: float | None,
    ) -> LLMResponse:
        body = self.request_body(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        estimate = (
            len(json.dumps(body["messages"]) + json.dumps(body.get("tools", []))) // CHARS_PER_TOKEN
        )
        if self.context_window and estimate + CONTEXT_HEADROOM_TOKENS > self.context_window:
            raise LLMError(
                f"the conversation (~{estimate:,} tokens) no longer fits the server's context "
                f"window ({self.context_window:,} tokens); raise it "
                "(Ollama: OLLAMA_CONTEXT_LENGTH=32768 ollama serve)"
            )
        status, data = self._call("POST", "/chat/completions", body)
        if status != 200:
            raise self._error(status, data, max_tokens=max_tokens)
        response = self._parse(data, body, tools)
        self._requests += 1
        if self._requests == 1 and not self._usage_estimated:
            self._check_truncation(response.usage, estimate)
        return response

    def _check_truncation(self, usage: Usage, estimate: int) -> None:
        """First request only (later ones may be partly served from a KV cache and report
        fewer prompt tokens). The estimate undercounts (4 chars per token), so a prompt that
        reports well below it was cut."""
        reported = usage.input_tokens + usage.cache_read_tokens
        if estimate > 2000 and reported < estimate * TRUNCATION_RATIO:
            raise LLMError(
                f"the server read only {reported:,} of ~{estimate:,} prompt tokens: it is "
                "truncating the prompt to its context window; raise it "
                "(Ollama: OLLAMA_CONTEXT_LENGTH=32768 ollama serve)"
            )

    def _parse(self, data: Any, body: dict[str, Any], tools: Sequence[ToolSpec]) -> LLMResponse:
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (TypeError, KeyError, IndexError):
            raise LLMError(f"unexpected response from {self.endpoint}: no choices") from None
        content = message.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = content or ""
        calls = [_tool_call(c) for c in message.get("tool_calls") or []]
        finish = choice.get("finish_reason") or ""
        out_chars = len(text) + sum(len(json.dumps(c.input)) for c in calls)
        several = 0
        if not calls and text.strip() and finish != "content_filter" and not self.strict_tool_calls:
            parsed = parse_text_tool_call(text, tools)
            if parsed:
                calls, text = [parsed], ""  # replayed as a structured call
                self.text_tool_calls_seen = True
                if TEXT_CALLS_NOTE not in self.notes:
                    self.notes.append(TEXT_CALLS_NOTE)
            else:
                several = count_text_tool_calls(text, tools)
        stop = "tool_use" if calls else _FINISH.get(finish, "other")
        if stop == "tool_use" and not calls:
            stop = "end_turn"
        usage_data = data.get("usage") if isinstance(data.get("usage"), dict) else None
        if usage_data and isinstance(usage_data.get("prompt_tokens"), int):
            details = usage_data.get("prompt_tokens_details") or {}
            cached = (details.get("cached_tokens") or 0) if isinstance(details, dict) else 0
            usage = Usage(
                input_tokens=max(0, usage_data["prompt_tokens"] - cached),
                output_tokens=usage_data.get("completion_tokens") or 0,
                cache_read_tokens=cached,
            )
        else:
            self._usage_estimated = True
            note = "the server reported no token usage; tokens are estimated from characters"
            if note not in self.notes:
                self.notes.append(note)
            usage = Usage(
                input_tokens=len(json.dumps(body)) // CHARS_PER_TOKEN,
                output_tokens=out_chars // CHARS_PER_TOKEN,
            )
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            model=str(data.get("model") or self.model),
            raw_stop_reason=finish,
            refusal="content_filter" if stop == "refusal" else "",
            provider_blocks=[message],
            multiple_text_tool_calls=several,
        )

    # --- preflight --------------------------------------------------------------------------

    def preflight(self, *, require_tools: bool = True) -> None:
        """Fail fast if the model cannot make tool calls (unless `require_tools` is False, as in
        whole-file mode: then one tiny plain request checks the server and loads the model);
        learn Ollama's context window."""
        if not require_tools:
            body = self.request_body(
                system="You are a connectivity check.",
                messages=[Message("user", text="Reply with OK.")],
                tools=[],
                max_tokens=5,
                temperature=0.0,
            )
            status, data = self._call("POST", "/chat/completions", body)
            if status != 200:
                raise self._error(status, data)
            self._parse(data, body, [])
            self._read_context_window()
            return
        body = self.request_body(
            system="You are a connectivity check. Always answer by calling the ping tool.",
            messages=[Message("user", text="Call the ping tool with ok=true.")],
            tools=[_PING],
            max_tokens=200,
            temperature=0.0,
        )
        status, data = self._call("POST", "/chat/completions", body)
        if status != 200:
            raise self._error(status, data)
        response = self._parse(data, body, [_PING])
        if not any(c.name == "ping" for c in response.tool_calls):
            looks_like_json = '"name"' in response.text or "ping" in response.text
            detail = (
                " It wrote the call as text instead, so the server is not parsing its tool "
                "calls (the model or its chat template lacks tool support)."
                if looks_like_json
                else ""
            )
            if looks_like_json and self.strict_tool_calls:
                detail += " Text-format tool calls are disabled by --strict-tool-calls."
            raise LLMError(
                f"model {self.model} did not make a tool call when asked to.{detail} Use a "
                f"tool-capable model. {MODEL_SUGGESTIONS}"
            )
        self._read_context_window()

    def _read_context_window(self) -> None:
        self.context_window = self._ollama_context_window()
        if self.context_window:
            self.notes.append(f"server context window: {self.context_window:,} tokens")

    def _ollama_context_window(self) -> int | None:
        """Ollama's /api/ps reports the loaded model's runtime context length; other servers
        don't have the endpoint, which is fine."""
        if not self.base_url.endswith("/v1"):
            return None
        try:
            status, data = self._call("GET", "/api/ps", None, retry=False, root=True)
        except LLMError:
            return None
        if status != 200 or not isinstance(data, dict):
            return None
        for m in data.get("models") or []:
            if not isinstance(m, dict):
                continue
            names = {m.get("name"), m.get("model")}
            ctx = m.get("context_length")
            if (self.model in names or f"{self.model}:latest" in names) and isinstance(ctx, int):
                return ctx
        return None


# --- conversion -------------------------------------------------------------------------------


def to_openai(messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or ""}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.input)},
                    }
                    for c in m.tool_calls
                ]
            out.append(msg)
            continue
        out += [
            {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
            for r in m.tool_results
        ]
        if m.text:
            out.append({"role": "user", "content": m.text})
    return out


def _tool_call(raw: Any) -> ToolCall:
    raw = raw if isinstance(raw, dict) else {}
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    call_id = str(raw.get("id") or f"call_{uuid.uuid4().hex[:12]}")
    name = str(fn.get("name") or "")
    args = fn.get("arguments")
    if isinstance(args, dict):  # some servers send an object, not a JSON string
        return ToolCall(call_id, name, args)
    if args in (None, ""):
        return ToolCall(call_id, name, {})
    try:
        parsed = json.loads(args)
    except (TypeError, ValueError) as exc:
        return ToolCall(call_id, name, {}, input_error=f"the arguments were not valid JSON ({exc})")
    if not isinstance(parsed, dict):
        return ToolCall(call_id, name, {}, input_error="the arguments must be a JSON object")
    return ToolCall(call_id, name, parsed)


# --- tool calls written as text -------------------------------------------------------------

# One optional wrapper per call; ```json is tried before a bare ``` fence.
_WRAPPERS = (("<tool_call>", "</tool_call>"), ("```json", "```"), ("```", "```"))
_SPACE = re.compile(r"\s*")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate keys")
    return dict(pairs)


_DECODER = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys)


def _text_json_values(text: str) -> list[Any] | None:
    """The JSON values `text` consists of, each bare or inside one fence / <tool_call> block,
    separated only by whitespace; None if anything else is in it."""
    values: list[Any] = []
    i = _SPACE.match(text).end()
    while i < len(text):
        closer = None
        for opener, close in _WRAPPERS:
            if text.startswith(opener, i):
                i, closer = i + len(opener), close
                break
        i = _SPACE.match(text, i).end()
        try:
            value, i = _DECODER.raw_decode(text, i)
        except ValueError:
            return None
        i = _SPACE.match(text, i).end()
        if closer is not None:
            if not text.startswith(closer, i):
                return None
            i = _SPACE.match(text, i + len(closer)).end()
        values.append(value)
    return values


def _call_shape(value: Any, tools: Sequence[ToolSpec]) -> tuple[ToolSpec, Any, bool] | None:
    """(tool, arguments, arguments were a JSON string) if `value` is {"name", "arguments"} naming
    an offered tool, with arguments an object or a string holding exactly one object."""
    if not isinstance(value, dict) or set(value) != {"name", "arguments"}:
        return None
    spec = next((t for t in tools if t.name == value["name"]), None)
    args = value["arguments"]
    if spec is None:
        return None
    if isinstance(args, str):
        try:
            args = _DECODER.decode(args)
        except ValueError:
            return None
        return (spec, args, True) if isinstance(args, dict) else None
    return (spec, args, False) if isinstance(args, dict) else None


def parse_text_tool_call(text: str, tools: Sequence[ToolSpec]) -> ToolCall | None:
    """The tool call if `text` is exactly one call to an offered tool with valid arguments."""
    values = _text_json_values(text)
    if not values or len(values) != 1:
        return None
    shape = _call_shape(values[0], tools)
    if shape is None:
        return None
    spec, args, as_string = shape
    if validate_tool_input(spec.input_schema, args) is not None:
        return None
    return ToolCall(
        f"call_{uuid.uuid4().hex[:12]}",
        spec.name,
        args,
        source="parsed_from_text",
        arguments_as_string=as_string,
    )


def count_text_tool_calls(text: str, tools: Sequence[ToolSpec]) -> int:
    """How many calls `text` holds if it is nothing but two or more call-shaped objects (none
    is run; the agent asks for one per reply), else 0."""
    values = _text_json_values(text) or []
    if len(values) >= 2 and all(_call_shape(v, tools) for v in values):
        return len(values)
    return 0


# --- rate limits ------------------------------------------------------------------------------


def parse_duration(text: str) -> float | None:
    """Seconds in a Go-style duration ("7.66s", "1m26.4s", "250ms", "2h0m"), else None."""
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    return sum(float(n) * _DURATION_UNIT_S[u] for n, u in parts)


def server_wait_s(
    headers: Mapping[str, str], message: str, now: float | None = None
) -> float | None:
    """How long a rate-limited server asks to wait: ``retry-after-ms``, ``Retry-After``
    (seconds or an HTTP date), or "try again in <duration>" in its message; None if it says
    nothing usable."""
    ms = headers.get("retry-after-ms", "").strip()
    try:
        if ms:
            return max(0.0, float(ms) / 1000)
    except ValueError:
        pass
    after = headers.get("retry-after", "").strip()
    if after:
        try:
            return max(0.0, float(after))
        except ValueError:
            pass
        try:
            when = email.utils.parsedate_to_datetime(after)
        except (TypeError, ValueError):
            when = None
        if when is not None and when.tzinfo is not None:
            return max(0.0, when.timestamp() - (time.time() if now is None else now))
    match = _TRY_AGAIN.search(message)
    return parse_duration(match.group(1)) if match else None


def _format_wait(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:,.0f} s" if seconds >= 10 else f"{seconds:,.1f} s"
    minutes = seconds / 60
    return f"{minutes:,.0f} min" if minutes < 120 else f"{minutes / 60:,.1f} h"
