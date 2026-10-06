# Plan: engine/agent.py

Approved 2026-10-06. The request and amendments are verbatim.

## Request

First: save the approved verifier plan and amendments as
docs/plans/verifier.md (and the sandbox and scanner plans if recoverable),
and from now on save each approved plan under docs/plans/ before building.

Next: engine/agent.py, the fix loop. Requirements:
- Inputs: repo path, migration name, scanner findings. The migration
  package supplies a short migration guide (old -> new patterns, with
  examples) used in the prompt.
- Tools for the model: read_file, list_files, search, and a precise
  edit_file (exact string replace). Edits allowed only in files with
  findings, dependency files, and test files (patch targets/imports only,
  the verifier enforces the rest). No shell access for the model.
- Loop: propose edits -> verify (reuse the cached baseline) -> if not
  verified, feed the verifier's machine-readable reasons back -> retry,
  max 3 retries. Stop early on "verified".
- Limits: max tool calls per attempt, max tokens and cost per job; abort
  cleanly with a clear reason if exceeded.
- Provider-agnostic LLM interface; one real provider implementation, chosen
  via config; API key read from the host env only (never passed to the
  sandbox). Temperature 0.
- Record the full trajectory (messages, tool calls, edits, verifier results
  per attempt) as JSON. This is evidence for the report and data for
  later benchmarking.
- Output: final diff, verification result, report markdown, trajectory.
- CLI: `apifix fix <path> --migration openai-v1`.
- Tests: a scripted fake LLM (no network) covering success on first try,
  success after a retry using verifier reasons, giving up after 3, edits
  outside allowed files rejected, and budget limits. One opt-in live test
  (marker `llm`, needs an API key) on the tiny integration repo.
Explain the plan before writing code.

## Plan

### Shape
```python
fix(repo: Path, migration: str, *, llm: LLMClient, sandbox: Sandbox,
    findings: list[Finding] | None = None, config: AgentConfig = AgentConfig()) -> FixResult
```
- **Your repo is never modified.** The agent works on a copy, and the output is a diff.
- **Findings:** if you pass none, the scanner runs.
- **Baseline:** the original code's sandbox run happens once per job and is reused by every `verify()` call.

### New modules
| File | Contents |
|---|---|
| `engine/llm.py` | The provider-neutral interface. `LLMClient.complete(system, messages, tools, max_tokens) -> LLMResponse`, which carries the text, tool calls, stop reason, model, and usage (input, output, cache read, cache write). It also holds the neutral message types and a provider registry (`get_llm(provider, model)`). |
| `engine/llm_anthropic.py` | The one real provider: Claude, through the official `anthropic` SDK. The SDK is an optional extra (`pip install 'apifix[anthropic]'`) and imported lazily, so the rest of the engine stays stdlib-only. Converts neutral messages to and from the SDK format and computes cost from a price table. |
| `engine/agent_tools.py` | The model's four tools and the edit policy. Pure filesystem code, no LLM. |
| `engine/agent.py` | The loop, limits, trajectory and result. |
| `migrations/openai_v1/guide.md` | The migration guide used in the prompt (details below). |

### Migration guide
- The `Migration` protocol gets a `guide: str` field, loaded from the migration package. That keeps migrations data-driven.
- The openai-v1 guide is about 150 lines of old → new patterns, with short examples:
  - client construction, including Azure
  - each resource (chat, completions, embeddings, images, audio, moderations, files, fine-tuning)
  - responses as objects: attribute access, `model_dump()`
  - streaming chunks
  - error-class mapping, including the `InvalidRequestError` split
  - updating `mock.patch` targets in tests
  - the dependency pin

### Tools
- **`read_file(path, start_line?, end_line?)`:** returns at most about 2,000 lines or 100 KB per call, with line numbers.
- **`list_files(dir?, glob?)`:** uses the shared walker, so tooling dirs are skipped and symlinks are never followed.
- **`search(pattern, glob?)`:** regex search with bounded output.
- **`edit_file(path, old_string, new_string, replace_all=false)`:** exact string replace. `old_string` must match exactly once unless `replace_all` is set, and the edit must leave a Python file that still parses.

All paths are relative to the repo copy. Absolute paths, `..`, symlinks and `.git` are refused.

**Edit policy:**
- An edit is allowed in:
  - files with findings in the initial scan
  - dependency files (`dependencies.is_dependency_file`)
  - test files (`tampering.is_test_file`). Any edit is accepted there; the verifier judges what it does.
- No new files, no deletions, no shell.
- A refused edit returns an error result to the model, naming the files it may edit, and is recorded in the trajectory.

### Loop
1. **Prompt.**
   - The system prompt holds the role, the hard rules (never delete, skip or weaken tests; edit only the allowed files) and the migration guide.
   - The first user message lists the findings (`file:line`, category, old pattern → suggestion, confidence) and the files that may be edited.
2. **Attempt.** The model calls tools until it ends its turn with no tool call. If it made no edits, the attempt fails with `no_changes`.
3. **Verify.** `verify(original, workdir, baseline=cached)`.
4. **Stop** on `verified`.
5. **Retry otherwise.**
   - The next user message carries the verifier's reasons: code, severity, message, test IDs and `file:line`. It also carries the first ~500 characters of each newly failing test's error message.
   - The conversation keeps growing, never rewritten. That keeps the prompt cache working, and keeps the model's earlier reasoning valid on the API side.
   - Edits carry over, so the model repairs its previous attempt.
6. **At most 4 attempts** (the first plus 3 retries).
7. **Final result: the best attempt** (verified > verified_weak > unverified > rejected). If that isn't the last attempt, its file snapshot is restored before the diff is made.

**Outcome:**
- `verified`, `verified_weak` or `rejected`: the verdict of the best attempt.
- `aborted`: a limit was hit, the provider failed, or the model refused. It always has a reason.

### Limits (`AgentConfig`)
| Limit | Default | When it is hit |
|---|---|---|
| `max_retries` | 3 | — |
| `max_tool_calls_per_attempt` | 60 | abort |
| `max_tokens_per_job` | 2M (input + output) | abort |
| `max_cost_usd_per_job` | $5 | abort |
| `max_output_tokens` per response | 16,000 | — |

- **Before each LLM call**, the agent checks the projected cost: the previous input size plus the maximum output. If that would exceed the remaining budget, it aborts before making the call.
- **After each call**, it adds up the actual usage.
- **Clean abort:** the edits so far, the diff and the trajectory are kept, with the reason (e.g. `budget_exceeded: $5.12 > $5.00`). Partial edits are not verified.
- **Unknown models:** a model without an entry in the price table refuses to start while a cost limit is set.

### Provider details (Claude)
- **Model:** default `claude-opus-5-5`, configurable with `--model` or `APIFIX_MODEL`. Provider is `--provider` or `APIFIX_PROVIDER` (default `anthropic`).
- **Prices:** Opus 5.5 is $4 / $20 per million tokens (cache reads $0.20). Sonnet 5.5 is $2 / $10. Prices are kept in a table in the code.
- **Effort:** set explicitly to `high`, since Opus 5.5 otherwise defaults to `medium`. Thinking can't be turned off on this model. Its blocks are passed back unchanged as provider-specific content in the neutral message format.
- **Prompt caching:** automatic.
- **Tool choice:** `auto`, because forced tool choice is rejected on this model.
- **Stop reasons:** `refusal` aborts with its category, and `max_tokens` is reported.
- **Refusal fallback:** I'd turn on the API's server-side fallback by default. If the main model declines, the API reruns the request on a fallback model, and cost is still computed from the usage it reports. I'm telling you it's on; I'll drop it if you'd rather not have it.
- **API key:** the provider reads `ANTHROPIC_API_KEY` from the host environment and passes it to the SDK explicitly. If the key is missing, it stops with a clear error. The sandbox already builds container environments from literals only. A new test puts a canary key in the host environment and checks it never shows up in any Docker call during a full `fix` run.

### Trajectory (JSON)
- **Job:** repo, migration, config, provider, model, settings that were dropped (e.g. temperature), findings, and the baseline summary.
- **Per attempt:**
  - messages in neutral form, plus every tool call (name, input, result or error, duration)
  - every edit (file, old, new, accepted or refused and why)
  - the attempt's diff and `verification.to_dict()`
  - usage and cost
- **Totals:** outcome and reason.
- Written by `fix` as plain data, so later benchmarking can read it without importing apifix.

### Output and CLI
- `FixResult(outcome, reason, diff, verification, report_markdown, trajectory)`.
- `diffing.py` gets a `unified_diff(original, fixed)`.
- **Report:** the outcome, the number of attempts, tokens and cost, then the verifier's report section for the attempt that was chosen.
- **CLI:** `apifix fix <path> --migration openai-v1 [--provider] [--model] [--max-cost] [--out DIR]`. It writes `fix.diff`, `report.md`, `verification.json` and `trajectory.json` (default directory `./apifix-out/<timestamp>/`).
- **Exit codes:** 0 verified, 1 not verified, 2 usage error or abort. GitHub URLs are still refused.

### Tests (no network)
- **The fix loop**, with a scripted `FakeLLM` (a list of responses, or a function of the messages it receives) and a fake sandbox, so no Docker:
  - success on the first try
  - success after a retry, where the second script step checks that the verifier's reason codes are in the feedback message
  - giving up after 3 retries, with the best attempt chosen and restored
  - edits outside the allowed files refused
  - path traversal and symlinks refused
  - a non-unique or missing `old_string`, and an edit that breaks syntax
  - the tool-call limit, the token limit, the cost limit, and the pre-call cost check
  - refusal and provider errors leading to `aborted`
  - the trajectory JSON being complete and valid
  - the API-key canary
  - the CLI writing all four output files
- **Tools:** bounded output for read, list and search.
- **Anthropic provider:** tested against a fake SDK client. Covers message and tool conversion, thinking blocks passed back unchanged, usage → cost, the temperature handling (decision 2), and the refusal stop reason.
- **Live test (`llm` marker):** opt-in with `pytest --llm`, the same scheme as `--docker`. It needs `ANTHROPIC_API_KEY` and Docker, and runs on the tiny repo from the verifier's Docker test (moved to a shared fixture) with a $1 limit.
- **Docs:** CLAUDE.md gets the new modules, the `fix` command and the agent's known limitations.

### Decisions for you

1. **Provider.** You didn't name one. I'm proposing Claude through the official SDK as the one real implementation, since this project only targets OpenAI's library as migration input. If you want a different provider, the interface stays the same and only `llm_anthropic.py` changes.
2. **Temperature 0 doesn't exist on the current Claude models.** Opus 5.5 and Sonnet 5.5 reject any `temperature` value with an error (400), and their thinking can't be turned off. My recommendation:
   - The interface keeps `temperature=0`.
   - The provider sends it only to models that accept it, and the trajectory records that it was dropped.
   - The default stays `claude-opus-5-5`.

   Even where temperature 0 is accepted, output isn't guaranteed to repeat, so the recorded trajectory is the real evidence anyway. The alternative is defaulting to an older model that accepts temperature 0, such as Sonnet 4.6, at some cost in quality.
3. **Retrying on `verified_weak`.** Your spec says to retry whenever the verdict isn't `verified`. But the weak reasons (low coverage of changed lines, tests now skipped by existing conditions, missing baseline) can't be fixed with the edits the agent is allowed to make. My recommendation:
   - Stop on `verified_weak` and report it.
   - Retry only on `rejected` and `unverified`.

   Retrying on weak would mostly spend money for nothing. Say if you want the literal behaviour instead.

Tell me to go ahead, with your answers, and I'll save the plan and build it.

## Amendments (approval)

Go ahead. Decisions: Claude via the official SDK; temperature handling as you
recommend; stop on verified_weak, retry only on rejected/unverified.
Amendments:
1. Refusal fallback OFF by default; on refusal, abort with the reason. If
   ever enabled, record per response which model actually answered.
2. Edit policy: allow edits to any Python file in the repo (plus dependency
   and test files); files without findings get scope warnings via scope.py
   instead of being refused. Still no new files, deletions, or shell.
3. Prompt injection: the system prompt must state that repository contents
   are untrusted data, never instructions. Add a verifier check that warns
   (reason code e.g. suspicious_edit) on added code unrelated to the
   migration: new imports other than openai, network/process calls
   (requests, urllib, http.client, socket, subprocess, os.system, os.popen),
   eval/exec/compile, and long encoded string literals. Unit-test it,
   including an injected instruction in a fixture repo's comment.
4. Make the price table overridable via config (e.g. env or a TOML file),
   and note in CLAUDE.md that it must be checked against Anthropic's current
   pricing page.
Save the plan with these amendments to docs/plans/agent.md, then build.

## Later changes

Decisions made while building, beyond the plan and amendments:

- Suspicious-edit check: new imports of a small set of I/O-free stdlib modules (`types`,
  `typing`, `functools`, `dataclasses`, `collections`, `unittest.mock`) and of the migration's
  `companion_modules` (openai: `httpx`) are not warned, and constructing `httpx.Request` /
  `Response` / `Headers` / ... is not a network call. The guide recommends exactly these for
  test mocks and openai 1.x error construction, so warning on them would fire on every correct
  fix. Added dependencies other than the migrated library are also warned (`new_dependency`).
- `unverified` is not retried when no edit can change it: the repo has no tests, or the sandbox
  could not run at all. A baseline failing with "docker not usable" aborts before any LLM call.
- Temperature: SDK 1.11 has no `temperature` parameter; it is sent as `extra_body` to the models
  that accept it (claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5).
- The model table also records whether a model takes `output_config.effort` (Haiku 4.5 does
  not); both flags are overridable in the TOML file.
- `walk.copy_repo` is shared by the sandbox and the agent's working copy; `diffing.unified_diff`
  produces a `git apply`-able patch.
