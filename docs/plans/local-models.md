# Plan: local / OpenAI-compatible model provider

Approved 2026-10-06. The request and amendments are verbatim.

## Request

Add a free local-model option (save the plan to
docs/plans/local-models.md first):
engine/llm_openai_compat.py: a provider for any OpenAI-compatible
chat-completions endpoint with tool calling (Ollama, LM Studio, vLLM,
hosted free tiers). Config: --provider openai-compat, --base-url, --model,
optional API key from env. Default base URL for Ollama
(http://localhost:11434/v1). Cost defaults to $0 for local endpoints;
token and tool-call limits still apply. Use plain HTTP with the stdlib or
httpx, no new heavy dependency. If the model doesn't support tool calls,
fail with a clear error.
Tests with fake HTTP responses; an opt-in live test (marker `local_llm`)
that runs against a local Ollama if one is reachable. Document it in README
and CLAUDE.md, including hardware expectations for local models.
Explain the plan before building.

## Plan

### Shape
`engine/llm_openai_compat.py`: `OpenAICompatLLM(model, base_url=..., api_key_env=..., ...)`
implements the existing `LLMClient` protocol, so `agent.py` and the verifier don't change
beyond two small hooks (below). It is registered in `get_llm` as `openai-compat`.

### HTTP
- **Stdlib `urllib.request`**, no new dependency. httpx is only installed with the `anthropic`
  extra, so it can't be relied on.
- One call per turn: `POST {base_url}/chat/completions` with `stream: false`.
- **Timeouts:** 600 s per request by default (`--timeout`), because local models are slow.
- **Retries:** two retries with backoff on connection resets, 429 and 5xx responses.
- **Bounds:** responses are capped at 20 MB, and redirects are not followed.
- **Clear errors:**
  - connection refused → "no server at http://localhost:11434/v1 (is Ollama running? `ollama serve`)"
  - 404 for the model → the error lists the models the server reports at `GET {base_url}/models`

### Wire format
- **Request:**
  - The system prompt becomes a `system` message.
  - Assistant turns carry `tool_calls` (`{"id", "type": "function", "function": {"name", "arguments": "<json>"}}`).
  - Tool results become `role: "tool"` messages with `tool_call_id`.
  - Tools are sent as `{"type": "function", "function": {name, description, parameters}}`, with `tool_choice: "auto"`.
  - `temperature` and `max_tokens` are passed through. Temperature 0 is honoured here, unlike on current Claude models.
- **Response:**
  - `choices[0].message.content` (may be null) and `tool_calls`.
  - `finish_reason` maps `stop` → `end_turn`, `tool_calls` → `tool_use`, `length` → `max_tokens`, and `content_filter` → `refusal`.
  - `usage.prompt_tokens` / `completion_tokens` (plus `prompt_tokens_details.cached_tokens` if present) feed the token limits.
  - `model` records which model answered.
- **Server quirks handled:**
  - A missing tool-call `id` gets a generated one.
  - Arguments that aren't valid JSON don't abort the job. `ToolCall` gets an `input_error` field, and the agent returns it to the model as a tool error so it can retry the call.
  - Reasoning fields (`reasoning_content`, `<think>`) are kept in the trajectory but not sent back.
  - If the server reports no usage, tokens are estimated from characters and the trajectory notes it.
- **Replay:** the raw assistant message is stored in `provider_blocks`, so the trajectory has exactly what the server returned. It's replayed as `content` + `tool_calls` only.

### Tool-call support (fail clearly)
- **Preflight:** an optional `LLMClient.preflight()` method, which `fix()` calls before the baseline sandbox run so a bad model fails in seconds, not after a Docker install. The openai-compat provider sends one tiny request asking the model to call a `ping` tool.
  - If the server rejects tools (Ollama returns 400 "... does not support tools"), the error is: "model X does not support tool calls; use a tool-capable model (e.g. qwen2.5-coder, llama3.1, mistral-nemo)".
  - If the reply has no `tool_calls`, the error is the same. Small models often print a JSON "call" as text instead; the error says so if the text looks like one.
- **Mid-run:** the same 400 mapping applies on every request.

### Cost
- **Local endpoints cost $0:** the base URL's host is loopback (`localhost`, `127.0.0.0/8`, `::1`) or a private LAN address (`10/8`, `172.16/12`, `192.168/16`, `*.local`). Token and tool-call limits still apply.
- **Non-local endpoints** (hosted free tiers): the price comes from the model table, the same TOML file as now (`[models."<model>"]`). With no price and a cost limit set, the existing rule applies: refuse to start, with the hint "add a price (0 for a free tier) or pass `--no-cost-limit`". Free tiers can quietly become paid, so I don't want to assume $0 (decision 1).
- **New flag:** `--no-cost-limit` (`max_cost_usd=None`). It's also how you'd run a priceless hosted model on purpose.

### API key
- **Optional**, read from the host environment: `APIFIX_LLM_API_KEY` by default, `--api-key-env NAME` to use a different variable, and no key if it's unset (Ollama and LM Studio don't need one).
  - I'm deliberately not reading `OPENAI_API_KEY`. apifix migrates OpenAI code, so that key is likely to be set, and it would be sent to whatever `--base-url` says (decision 2).
- **Key safety:**
  - The key is sent only over `https` or to a local host. Plain `http` to a remote host with a key is refused.
  - The key never appears in the trajectory, reports or error messages.
  - It never reaches the sandbox, which builds its environment from literals. The existing canary test is extended to this provider.

### Context-window truncation (silent failure to guard against)
- **The problem:** Ollama's default context window is small (a few thousand tokens) and it silently drops the start of longer prompts. That would cut off the system prompt and rules partway through a job.
- **The guard:** if a response's `prompt_tokens` is far below the agent's own estimate (less than half), the job aborts with "the server appears to truncate the prompt to ~N tokens; raise the context window (Ollama: `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`)".
- **Docs:** the README explains the setting.

### Config and CLI
```
apifix fix <path> --migration openai-v1 --provider openai-compat \
    [--base-url URL] --model NAME [--api-key-env VAR] [--timeout S] [--no-cost-limit]
```
- `--base-url` defaults to `$APIFIX_BASE_URL`, then `http://localhost:11434/v1`.
- `--model` is required for openai-compat (decision 3). If it's missing, the error lists the server's models.
- The trajectory records the provider, the base URL (with any userinfo removed) and the model, never the key.

### Tests (no network)
- **Provider tests** inject a fake transport (request → status, headers, body), covering:
  - request shape: messages, tools, `tool_call_id`, temperature
  - response parsing: text, tool calls, finish reasons, usage, a missing id, invalid JSON arguments
  - errors: 400 "does not support tools", 404 model with the model list, connection refused, a 5xx retry then success, an oversized response
  - preflight passing and failing (no tool calls, JSON-as-text)
  - local-host detection and $0 cost, and a remote host with no price
  - key handling: the custom env var, the key never logged, a key over remote `http` refused
  - the truncation guard
- **One test against a real `http.server`** on 127.0.0.1 in a thread, so the actual `urllib` code path runs too.
- **Agent tests:** the fake LLM runs the loop with `input_error` tool calls, a preflight failure leads to a clean abort before any sandbox run, and the API-key canary.
- **CLI:** `--provider openai-compat` wiring, `--no-cost-limit`, and the missing-model error.
- **Opt-in live tests:** marker `local_llm`, flag `--local-llm` (same scheme as `--docker` / `--llm`). They're skipped unless `GET {base}/models` answers and the model (`$APIFIX_LOCAL_MODEL`, default `qwen2.5-coder:7b`) is installed.
  - *Provider smoke test* (no Docker): preflight plus one tool round trip.
  - *Full `fix` of the tiny repo* (also needs `--docker`): asserts the job completes with a valid trajectory and at least one tool call. It doesn't require `verified`, since a 7B model may well not get there. If it does verify, the diff must contain the new call path.

### Docs
- **README:** local setup (install Ollama, `ollama pull qwen2.5-coder:7b`, raise the context window, run `apifix fix --provider openai-compat --model ...`), LM Studio and vLLM base URLs, hosted free tiers with prices in TOML, and the hardware table below.
- **CLAUDE.md:** layout, dev commands, the key rule, and known limitations: quality of small models, truncation, servers that don't report usage, and that the tool-support check is heuristic.

**Hardware expectations** (rough, 4-bit quantised models, which is what Ollama pulls by default):

| Model size | Example | Memory for weights | Practical setup | Speed / quality |
|---|---|---|---|---|
| 7-8B | qwen2.5-coder:7b, llama3.1:8b | ~5 GB | 8 GB VRAM GPU, or 16 GB RAM CPU-only | GPU: fast; CPU: slow (a job can take an hour). Fine for the tiny repo, unreliable on real ones |
| 14B | qwen2.5-coder:14b | ~9 GB | 12-16 GB VRAM, or 32 GB RAM / Apple Silicon | Noticeably better tool use |
| 32B | qwen2.5-coder:32b | ~20 GB | 24 GB VRAM GPU, or 32-48 GB unified-memory Mac | Smallest size I'd suggest for real repos |
| 70B+ | llama3.3:70b | ~40 GB+ | 48 GB+ VRAM or a 64 GB+ Mac | Best local quality, slow |

Add memory for the context: an agent conversation reaches 30-100k tokens, so a 32k context needs a few more GB of KV cache. Expect more retries and more `rejected` results than with Claude. The verifier's checks are identical, so a wrong fix is still caught, not shipped.

### Decisions for you
1. **Hosted endpoints without a price:** refuse unless you add a price or pass `--no-cost-limit` (recommended), or treat every endpoint as $0 with a warning.
2. **API key variable:** a dedicated `APIFIX_LLM_API_KEY` plus `--api-key-env` (recommended), or `OPENAI_API_KEY`.
3. **Model for openai-compat:** required (recommended, since it depends on what you've pulled), or default to `qwen2.5-coder:7b`.

## Amendments (approval)

Approved. Decisions: refuse unpriced hosted endpoints unless a price or
--no-cost-limit is given; use APIFIX_LLM_API_KEY only; make --model
required, and have the docs and the error message suggest a starting
model per hardware size from the table.
Amendments:
1. When the endpoint is not loopback/private, print a one-line notice that
   repository code will be sent to that third-party server, and add a
   README note that some hosted free tiers may use submitted data for
   training.
2. In the local_llm full-fix test, record the verdict, attempt count, and
   token usage in the test output so results are visible, while still
   asserting only that the job completes cleanly.
Add both to docs/plans/local-models.md, then build and commit once
pytest -q --docker and ruff pass.

## Later changes

Decisions made while building:

- Context-window truncation is guarded three ways instead of one. Ollama may report only the
  newly evaluated prompt tokens when it reuses its KV cache, so comparing reported and
  estimated prompt tokens on every request would misfire. Instead: (1) for Ollama, preflight
  reads the loaded model's runtime context window from `/api/ps`, and every request whose
  estimated prompt (plus 1,024 tokens for the reply) does not fit fails before it is sent;
  (2) on any server, the first request's reported prompt tokens are compared with the estimate
  (below 70% means truncation); (3) explicit "maximum context length" errors are mapped to a
  clear message.
- The third-party notice (amendment 1) is printed by the CLI to stderr and also recorded in the
  provider's notes, so it appears in the trajectory and report.
- The local_llm full-fix test (amendment 2) prints a one-line JSON summary (`-s`) and attaches
  the same fields with `record_property` (visible with `-rA` or in JUnit XML). It fails only on
  provider-side aborts (preflight / provider errors), not on a weak verdict or a limit.
- A test checks that the README and CLAUDE.md hardware tables name the same models as the
  error message's suggestions.
