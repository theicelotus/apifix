"""Claude via the official ``anthropic`` SDK (optional extra: ``pip install 'apifix[anthropic]'``).

- The API key is read from the host environment (``ANTHROPIC_API_KEY``) and given to the SDK
  explicitly. It never goes anywhere else; sandboxes build their environment from literals.
- Assistant turns are replayed with their original content blocks, so thinking blocks go back
  unchanged and the conversation stays append-only.
- ``temperature`` is sent only to models that accept sampling parameters; current models reject
  it, and the dropped setting is recorded in ``notes``.
- Server-side refusal fallback is off by default. When enabled, each response records the model
  that actually answered (``LLMResponse.model``) and is priced at that model's rates.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from apifix.engine.llm import (
    ANTHROPIC_MODELS,
    OUTAGE_WAITS_S,
    LLMError,
    LLMResponse,
    Message,
    ModelInfo,
    OutageWaits,
    StopReason,
    ToolCall,
    ToolSpec,
    Usage,
)

API_KEY_ENV = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


class AnthropicLLM:
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        client: Any = None,
        models: Mapping[str, ModelInfo] = ANTHROPIC_MODELS,
        effort: str | None = "high",
        fallback: bool = False,
        timeout_s: float = 600.0,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.models = dict(models)
        self.effort = effort
        self.fallback = fallback
        self.notes: list[str] = []
        # the SDK retries a failed connection a few times within seconds; once the API has
        # answered in this job, a longer outage is waited out too
        self.answered = False
        self.outage = OutageWaits(self.notes, sleep)
        if client is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise LLMError(f"{API_KEY_ENV} is not set")
            try:
                import anthropic
            except ImportError as exc:
                raise LLMError(
                    "the anthropic SDK is not installed: pip install 'apifix[anthropic]'"
                ) from exc
            client = anthropic.Anthropic(api_key=key, timeout=timeout_s, max_retries=max_retries)
        self.client = client
        if model not in self.models:
            self.notes.append(f"{model} is not in the model table: no prices, effort not sent")

    @property
    def info(self) -> ModelInfo | None:
        return self.models.get(self.model)

    def cost(self, usage: Usage, model: str | None = None) -> float | None:
        info = self.models.get(model or self.model)
        return info.cost(usage) if info else None

    # --- request ------------------------------------------------------------------------------

    def request(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        """The keyword arguments for `messages.create` (pure; unit-tested)."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system}],
            "messages": [to_anthropic(m) for m in messages],
            "cache_control": {"type": "ephemeral"},  # cache the growing conversation prefix
        }
        if tools:  # whole-file mode offers none
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
        info = self.info
        if self.effort and info and info.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if temperature is not None:
            if info and info.temperature:
                kwargs["extra_body"] = {"temperature": temperature}
            else:
                self._note(f"temperature={temperature} not sent: {self.model} does not accept it")
        if self.fallback:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _note(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note)

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        temperature: float | None,
    ) -> LLMResponse:
        kwargs = self.request(
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        create = self.client.beta.messages.create if self.fallback else self.client.messages.create
        outage = list(OUTAGE_WAITS_S) if self.answered else []
        waited: list[float] = []
        while True:
            try:
                response = create(**kwargs)
                break
            except Exception as exc:
                if outage and _is_connection_error(exc):
                    waited.append(outage.pop(0))
                    self.outage.wait(waited[-1], f"{type(exc).__name__}: {exc}")
                    continue
                if _is_sdk_error(exc):
                    after = OutageWaits.gave_up(waited) if waited else ""
                    raise LLMError(f"{type(exc).__name__}: {exc}{after}") from exc
                raise
        self.answered = True
        return from_anthropic(response)


def _is_connection_error(exc: Exception) -> bool:
    """The API could not be reached (not a timeout: then it was reached, and is slow)."""
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(exc, anthropic.APIConnectionError) and not isinstance(
        exc, anthropic.APITimeoutError
    )


def _is_sdk_error(exc: Exception) -> bool:
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(exc, anthropic.AnthropicError)


# --- conversion -----------------------------------------------------------------------------


def to_anthropic(m: Message) -> dict[str, Any]:
    if m.role == "assistant":
        if m.provider == AnthropicLLM.provider and m.provider_blocks is not None:
            return {"role": "assistant", "content": m.provider_blocks}
        content: list[dict[str, Any]] = []
        if m.text:
            content.append({"type": "text", "text": m.text})
        content += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in m.tool_calls
        ]
        return {"role": "assistant", "content": content}
    blocks: list[dict[str, Any]] = [
        {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content}
        | ({"is_error": True} if r.is_error else {})
        for r in m.tool_results
    ]
    if m.text:
        blocks.append({"type": "text", "text": m.text})
    return {"role": "user", "content": blocks}


def from_anthropic(response: Any) -> LLMResponse:
    blocks = [b.model_dump(mode="json", exclude_none=True) for b in response.content]
    text = "\n".join(b["text"] for b in blocks if b.get("type") == "text" and b.get("text"))
    calls = [
        ToolCall(b["id"], b["name"], b.get("input") or {})
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    u = response.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
    )
    raw = response.stop_reason or ""
    refusal = ""
    if raw == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) or "unspecified"
        explanation = getattr(details, "explanation", None)
        refusal = f"{category}: {explanation}" if explanation else category
    return LLMResponse(
        text=text,
        tool_calls=calls,
        stop_reason=_STOP_REASONS.get(raw, "other"),
        usage=usage,
        model=response.model,
        raw_stop_reason=raw,
        refusal=refusal,
        provider_blocks=blocks,
    )
