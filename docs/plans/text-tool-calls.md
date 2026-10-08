# Plan: tool calls written as text (openai-compat)

Approved 2026-10-07. The request and amendments are verbatim.

## Request

Go with option 2: write a plan (save to docs/plans/text-tool-calls.md),
then explain it before building. Requirements:
- Only in the openai-compat provider. Accept a text reply as a tool call
  only if the entire reply (trimmed, optionally inside one ```json fence)
  is a single JSON object {"name": ..., "arguments": {...}} (also accept
  the same inside <tool_call> tags if they appear without being parsed).
  No surrounding prose, no multiple objects.
- name must exactly match an offered tool; arguments must pass the same
  validation as structured tool calls; otherwise treat it as plain text.
- On by default for openai-compat, with a flag to disable it
  (--strict-tool-calls). The preflight check should pass for such models
  but report that text-format calls are being used.
- Record each such call in the trajectory with source "parsed_from_text",
  and count them in the report.
- Tests: valid bare JSON call, fenced call, prose + JSON (rejected),
  unknown tool name (rejected), invalid arguments (rejected), two objects
  (rejected), strict flag disables it, and the trajectory marking.

## Background

`qwen2.5-coder-7b`, using Ollama's official template, always replies with a bare
`{"name": "read_file", "arguments": {...}}` and leaves out the `<tool_call>` tags. Ollama's
parser needs those tags, so it returns the call as `content` with no `tool_calls`. The provider
then sees plain text: preflight fails, and in a job the agent would treat the reply as the end
of its turn.

## Plan

### Parsing (`llm_openai_compat.py`)
`_parse(data, body, tools)` gets the tools that were offered. It falls back to parsing text only
when **all** of these hold:

- text parsing is on (not `strict_tool_calls`),
- the server returned no structured `tool_calls`,
- the reply is not a refusal (`finish_reason` is not `content_filter`),
- the text is non-empty.

`parse_text_tool_call(text, tools) -> ToolCall | None`, a pure function:

1. `t = text.strip()`.
2. Remove at most **one** wrapper, and only if it spans the whole of `t`:
   - a fence: ```` ```json ```` or ```` ``` ```` + newline … closing ```` ``` ````, or
   - `<tool_call>` … `</tool_call>`.

   Then strip again. No nesting (a fence inside tags is plain text), and the inside must not
   contain another fence or tag.
3. `json.loads` on the whole remainder. Anything after the object ("Extra data") fails, so
   `{..}{..}` or `{..}\n{..}` is plain text. Duplicate keys are rejected with
   `object_pairs_hook` (ambiguous).
4. The value must be a dict with exactly the keys `name` and `arguments`. `name` must be a
   string equal to an offered tool's name (case-sensitive). `arguments` must be a JSON object;
   a JSON-encoded string is not accepted.
5. `arguments` must pass `validate_tool_input(spec.input_schema, arguments)` (below). Any
   failure means plain text.

If parsing succeeds, the response is:

- `tool_calls=[ToolCall(call_<uuid>, name, args, source="parsed_from_text")]`,
- `stop_reason="tool_use"`,
- `text=""`.

The text is cleared so that history replays it as a normal structured assistant tool call. The
call is not duplicated as text, and Ollama renders the replayed call in its correct
`<tool_call>` form, which shows the model the expected format. `provider_blocks` keeps the raw
server message, and character-based usage estimates are computed before the text is cleared.

If parsing fails, the reply stays plain text exactly as today. Nothing is half-parsed.

### Shared argument validation (`llm.py`, `agent_tools.py`)
Today structured calls are checked in `agent_tools._arguments`: a hand-written type table that
duplicates the tool schemas. It rejects unknown keys and wrong types, but **not missing
required arguments**. A call like `read_file {}` or `{"path": null}` currently crashes the agent
with an uncaught `TypeError` (checked on the current code).

- New `validate_tool_input(schema, args) -> str | None` in `llm.py`. It returns an error message
  or None, and covers the JSON-schema subset our tools use:
  - `type: object`
  - `properties` with `string` / `integer` / `boolean` (bool is not an integer)
  - `required`
  - unknown keys rejected
  - `null` allowed only for optional keys (same as today)
- `agent_tools._arguments` uses it with each tool's `input_schema` (and drops `_ARGS`), raising
  `ToolError` on failure. Both paths now use the same validator, and the missing-argument crash
  becomes an ordinary tool error sent back to the model.
- Structured calls keep today's behaviour of sending an error back to the model. Text calls that
  fail validation are plain text, as required.

### Marking (`llm.py`, `agent.py`)
- `ToolCall` gets `source: str = "structured"`. Text-parsed calls have `"parsed_from_text"`.
  `llm_anthropic.py` is unchanged and uses the default.
- `agent.py` adds `"source": call.source` to each entry in `attempt.tool_calls` (trajectory
  `attempts[].tool_calls[]`). The `messages` list gets it automatically through `asdict`.
- The report gets a line only when some calls were parsed:
  `- Tool calls parsed from text: N of M (the model wrote them as text; --strict-tool-calls
  disables this)`.
- The trajectory `job` section also records `"strict_tool_calls": true|false` (via
  `getattr(llm, "strict_tool_calls", None)`).

### Preflight
The ping check uses the same `_parse`:

- **Structured call:** passes as today.
- **Text call, parsing on:** passes and adds a note (shown in the report and trajectory):
  `model <m> writes tool calls as text; apifix parses replies that are exactly one JSON call
  (--strict-tool-calls disables this)`. The note is also added during the job the first time a
  call is parsed from text, for servers where preflight did not trigger it.
- **Text call, strict:** fails as today, and the error adds "text-format calls are disabled by
  --strict-tool-calls".
- Anything else (prose, wrong tool) fails as today.

### Config and CLI
- `OpenAICompatLLM(..., strict_tool_calls: bool = False)`.
- `get_llm` passes it through. With `--provider anthropic` it is rejected like `base_url`
  ("only applies to --provider openai-compat").
- `apifix fix --strict-tool-calls`: openai-compat only; it errors with the anthropic provider.
- No environment variable (not requested).

### Safety
This adds no new capability. The model could already call these same four tools (no shell)
through structured calls, and parsed calls go through the same tool sandboxing, edit limits,
verifier, tampering and suspicious-edit checks. A reply counts only if it is entirely one
well-formed call to an offered tool. JSON quoted inside prose, or copied out of a repository
file into a longer reply, is never executed.

### Tests (no network)
`tests/test_llm_openai_compat.py` (fake transport):

- **Accepted:**
  - bare JSON call (with surrounding whitespace)
  - ```` ```json ```` fenced call
  - plain ```` ``` ```` fence
  - `<tool_call>` tags
- **Rejected (stays plain text):**
  - prose + JSON
  - JSON + trailing prose
  - unknown tool name
  - wrong-case name
  - invalid arguments: wrong type, unknown key, missing required key, `arguments` as a string,
    extra top-level key
  - two objects
  - a JSON array
  - a fence inside tags
  - duplicate keys
- Structured `tool_calls` present → text is never parsed.
- `strict_tool_calls=True` → a valid bare call stays text.
- Parsed call: `source == "parsed_from_text"`, `stop_reason == "tool_use"`, `text == ""`.
  Replaying it through `to_openai` gives a structured assistant `tool_calls` entry.
- Preflight: passes with the note for a text ping; fails in strict mode with the strict hint.

`tests/test_agent.py`:

- A scripted openai-compat model replies with a bare-JSON `edit_file` call. The trajectory's
  `attempts[].tool_calls[].source` is `parsed_from_text` (structured calls are `structured`),
  and the report shows the count line.
- A structured call missing a required argument becomes a tool error, not a crash (regression).

`tests/test_agent_tools.py`: `validate_tool_input` cases, and `_arguments` behaviour unchanged
for valid input.

`tests/test_cli.py`:

- `--strict-tool-calls` reaches the provider.
- It is rejected with `--provider anthropic`.

The live `local_llm` test needs no change. After building, I run it against
`APIFIX_LOCAL_MODEL=qwen2.5-coder-7b` (free, slow) and report the result.

### Docs
- CLAUDE.md: add the flag to the layout line, and a "Local / OpenAI-compatible models"
  limitation describing exactly what is accepted. Multiple calls written as text in one reply
  are not parsed: the reply is plain text, which ends the agent's turn.
- README: add the same to the local-models section.

### Decisions for you
1. Multiple bare objects in one reply are rejected, as required, which ends the turn. Fine as
   is, or should the agent instead tell the model "one tool call per reply"?
2. `arguments` given as a JSON-encoded string (some models do this) is rejected under "arguments:
   {...}". Keep it strict?

## Amendments (approval)

Approved, with these decisions:
1. Two or more calls as bare JSON: execute none of them, reply to the
   model "Send exactly one tool call per reply." and let it continue.
   Cap at 3 such nudges per attempt (then treat as plain text, as now),
   and record each nudge in the trajectory.
2. "arguments" given as a JSON string: accept it only if the string
   parses to exactly one JSON object; then apply the same validation.
   Anything else is rejected. Mark these calls in the trajectory too.
Update docs/plans/text-tool-calls.md, build it, run pytest -q --docker
and ruff, commit, then run the live test against qwen2.5-coder-7b and
report the result.

### How the amendments are built

1. **Several calls in one reply.** The provider splits the text into JSON values, each bare or
   inside its own single fence / `<tool_call>` block, separated only by whitespace. If there are
   two or more and every one is call-shaped (keys exactly `name` + `arguments`, `name` an offered
   tool, `arguments` an object or a string that decodes to one), the response carries no tool
   calls and `LLMResponse.multiple_text_tool_calls = n`. Nothing is executed. The agent appends
   the user message "Send exactly one tool call per reply." and continues the attempt; each
   nudge is recorded in `attempts[].nudges` (`kind`, number of calls, response index). After 3
   nudges in an attempt (`AgentConfig.max_one_call_nudges`) the reply is plain text, which ends
   the turn as before. The report counts the nudges. Prose mixed in, or a JSON array, is
   plain text (no nudge). Off in strict mode.
2. **`arguments` as a JSON string.** Accepted only if the string decodes (whole string,
   duplicate keys rejected) to one JSON object, which then goes through
   `validate_tool_input`. Such calls get `ToolCall.arguments_as_string = True`, recorded in the
   trajectory's tool-call entries and counted in the report line.

## Later changes

### 2026-10-07: tool-call example and no-call nudge for text-format models

Request (verbatim):

> For text-format models (openai-compat, non-strict mode), make two changes:
>
> Add one short example of a valid tool call (a read_file call on a repo file) to the system
> prompt. Only add it when tool calls are parsed from text, not in strict mode or for the
> Anthropic provider.
> Add a no-call nudge. If a reply in an attempt contains no tool call and the task isn't
> finished, reply: "You can't see files directly. Call a tool, for example read_file, to get
> them." Use the same cap (3 per attempt) and record it in attempts[].nudges with a type that
> distinguishes it from the multi-call nudge.
>
> Add tests for both, including that strict mode and the Anthropic provider are unaffected. Run
> the full suite with --docker and ruff, then commit. Don't run the live tests.

How it is built:

- **Which models count as text-format.** The provider sets `text_tool_calls_seen = True` the
  first time it parses a call from text, which can be the preflight ping. In strict mode it
  never parses, so the flag stays False. The Anthropic provider has no such attribute. The
  agent reads it with `getattr(llm, "text_tool_calls_seen", False) is True`, so `agent.py`
  stays provider-neutral. A model that makes structured calls (also openai-compat, non-strict)
  gets neither change, because a JSON-in-text example would only push it towards the weaker
  format.
- **Example.** Right after a successful preflight, if the flag is set, the system prompt gets
  one paragraph appended: "Tool calls: reply with exactly one JSON object and nothing else, for
  example:" followed by `{"name": "read_file", "arguments": {"path": "<first finding's
  file>"}}` (a real repo file; findings are sorted). The system prompt is fixed for the whole
  job, so models detected only later in the job do not get it. The trajectory already records
  `system_prompt`; `job.tool_call_example` records whether it was added.
- **No-call nudge.** In `_converse`, after the multi-call nudge and the max-tokens
  continuation: when the reply has no tool call, the flag is set, and the attempt has no
  accepted edit yet ("not finished", the same test that marks an attempt `no_changes`), the
  agent replies "You can't see files directly. Call a tool, for example read_file, to get
  them." and continues. The cap is `AgentConfig.max_no_call_nudges = 3` per attempt, counted
  separately from the multi-call nudges. Each nudge is recorded in `attempts[].nudges` as
  `{"kind": "no_tool_call", "response": i, "message": ...}`. A reply without a call after an
  accepted edit still ends the turn, and so does one after the cap. The report counts these
  nudges.
