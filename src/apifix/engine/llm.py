"""Provider-neutral LLM interface for the fix agent.

The agent speaks only these types; a provider module converts them to and from its SDK. Every
message keeps the provider's own content blocks (``provider_blocks``) so that a provider can
replay its responses unchanged (e.g. Claude's thinking blocks), and so the trajectory records
exactly what the model returned.

Model facts (prices, which request options a model accepts) live in a table that can be
overridden from a TOML file, because prices change: check them against the provider's current
pricing page.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]
PRICES_ENV = "APIFIX_PRICES"


class LLMError(RuntimeError):
    """The provider failed (network, auth, rate limit after retries, bad request, ...)."""


class RequestTooLarge(LLMError):
    """The server refused one request as larger than its per-minute token limit, which it
    counts as prompt plus the reply tokens the request allows for (`max_tokens`). `limit` and
    `requested` are the server's figures; a request with fewer reply tokens may fit."""

    def __init__(self, message: str, *, limit: int, requested: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.requested = requested


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    # Set when the provider could not decode the arguments (e.g. invalid JSON); the agent sends
    # it back to the model as a tool error instead of running the tool.
    input_error: str = ""
    # "structured" (the server's tool-call field) or "parsed_from_text" (the model wrote the
    # call as its reply text; openai-compat only).
    source: str = "structured"
    arguments_as_string: bool = False  # parsed from text with "arguments" as a JSON string


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: Literal["user", "assistant"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Assistant turns: the provider's raw content blocks, replayed verbatim by that provider.
    provider: str | None = None
    provider_blocks: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: Usage
    model: str  # the model that actually answered
    raw_stop_reason: str = ""
    refusal: str = ""  # category / explanation when stop_reason == "refusal"
    provider_blocks: list[dict[str, Any]] | None = None
    # The reply was nothing but this many (>= 2) tool calls written as text; none were parsed.
    multiple_text_tool_calls: int = 0


class LLMClient(Protocol):
    provider: str
    model: str
    # Request settings that could not be applied (e.g. "temperature not supported by <model>").
    notes: list[str]

    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        temperature: float | None,
    ) -> LLMResponse: ...

    def cost(self, usage: Usage, model: str | None = None) -> float | None:
        """US dollars for `usage` on `model` (default: the configured one); None if unknown."""
        ...


# Optional provider methods, looked up with getattr by the agent:
#   preflight() -> None        cheap check before the job starts (raise LLMError to stop it)
#   endpoint: str | None       where requests go (recorded in the trajectory; no credentials)


# --- tool input -------------------------------------------------------------------------------

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_tool_input(schema: Mapping[str, Any], args: Any) -> str | None:
    """Why `args` does not fit a tool's input schema, or None if it does. Covers the subset the
    tools use: an object of typed properties with required keys; unknown keys are rejected and
    null counts as absent."""
    if not isinstance(args, dict):
        return "tool input must be an object"
    properties = schema.get("properties") or {}
    unknown = set(args) - set(properties)
    if unknown:
        return f"unknown arguments: {sorted(unknown)}"
    missing = [k for k in schema.get("required") or [] if args.get(k) is None]
    if missing:
        return f"missing required arguments: {missing}"
    for key, value in args.items():
        kind = properties[key].get("type") if isinstance(properties[key], dict) else None
        expected = _JSON_TYPES.get(kind) if isinstance(kind, str) else None
        if value is None or expected is None:
            continue
        if not isinstance(value, expected) or (isinstance(value, bool) and kind != "boolean"):
            return f"{key} must be {kind}"
    return None


# --- model table ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelInfo:
    """Per-model facts. Prices are US dollars per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float  # 5-minute cache writes
    temperature: bool = False  # accepts sampling parameters
    effort: bool = True  # accepts output_config.effort

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * self.cache_read
            + usage.cache_write_tokens * self.cache_write
        ) / 1_000_000


# Anthropic first-party prices as published 2026-09 (5-minute cache writes at 1.25x input).
# CHECK AGAINST https://www.anthropic.com/pricing BEFORE RELYING ON COST LIMITS; override with a
# TOML file (see load_model_table) when they change.
ANTHROPIC_MODELS: dict[str, ModelInfo] = {
    "claude-fable-5-1": ModelInfo(10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": ModelInfo(4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": ModelInfo(5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": ModelInfo(2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": ModelInfo(2.0, 10.0, 0.20, 2.5),
    "claude-opus-4-6": ModelInfo(5.0, 25.0, 0.50, 6.25, temperature=True),
    "claude-sonnet-4-6": ModelInfo(3.0, 15.0, 0.30, 3.75, temperature=True),
    "claude-haiku-4-5": ModelInfo(1.0, 5.0, 0.10, 1.25, temperature=True, effort=False),
}

_FIELDS = {"input", "output", "cache_read", "cache_write", "temperature", "effort"}


def load_model_table(
    base: Mapping[str, ModelInfo], path: str | os.PathLike[str] | None = None
) -> dict[str, ModelInfo]:
    """`base` overridden by a TOML file (`path`, else $APIFIX_PRICES if set):

        [models."claude-opus-5-5"]
        input = 4.0
        output = 20.0
        cache_read = 0.2
        cache_write = 5.0

    A model already in the table may set only some fields; a new model must set all four
    prices.
    """
    table = dict(base)
    path = path if path is not None else os.environ.get(PRICES_ENV) or None
    if path is None:
        return table
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read model prices from {path}: {exc}") from exc
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"{path}: [models] must be a table")
    for name, fields in models.items():
        if not isinstance(fields, dict) or not set(fields) <= _FIELDS:
            raise ValueError(f"{path}: models.{name}: allowed keys are {sorted(_FIELDS)}")
        for key, value in fields.items():
            kind = bool if key in ("temperature", "effort") else (int, float)
            if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
                raise ValueError(f"{path}: models.{name}.{key} has the wrong type")
        if name in table:
            table[name] = replace(table[name], **fields)
        else:
            missing = {"input", "output", "cache_read", "cache_write"} - set(fields)
            if missing:
                raise ValueError(f"{path}: new model {name} needs {sorted(missing)}")
            table[name] = ModelInfo(**fields)
    return table


# --- registry -------------------------------------------------------------------------------

PROVIDERS = ("anthropic", "openai-compat")
DEFAULT_PROVIDER = "anthropic"
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5"}  # openai-compat: --model is required


def get_llm(
    provider: str | None = None,
    model: str | None = None,
    *,
    prices: str | os.PathLike[str] | None = None,
    **options: Any,
) -> LLMClient:
    """Build the configured provider. Defaults: $APIFIX_PROVIDER / $APIFIX_MODEL, then
    anthropic / claude-opus-5-5. openai-compat takes base_url, api_key_env, timeout_s and
    strict_tool_calls."""
    provider = provider or os.environ.get("APIFIX_PROVIDER") or DEFAULT_PROVIDER
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r} (available: {', '.join(PROVIDERS)})")
    model = model or os.environ.get("APIFIX_MODEL") or DEFAULT_MODELS.get(provider)
    if provider == "openai-compat":
        from apifix.engine.llm_openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(model, models=load_model_table({}, prices), **options)
    unsupported = set(options) & {"base_url", "api_key_env", "strict_tool_calls"}
    if unsupported:
        raise ValueError(f"{', '.join(sorted(unsupported))} only apply to --provider openai-compat")
    from apifix.engine.llm_anthropic import AnthropicLLM

    return AnthropicLLM(model, models=load_model_table(ANTHROPIC_MODELS, prices), **options)
