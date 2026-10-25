# llmflows run 2: 0.x per-call kwargs through wrappers and on 1.x calls

## Request

Context: Read CLAUDE.md. llmflows run ~/apifix-runs/llmflows-gptoss-2/ is verified_weak, but llmflows/llms/azure_openai.py still passes engine=self._engine (unchanged lines, fix.diff lines 65 and 89) to client.completions.create through call_with_retry(func=..., engine=...). 1.x methods don’t accept engine= (Azure takes the deployment as model=), so real calls raise TypeError; the tests mock call_with_retry and the scanner never flagged it. Record this request verbatim in docs/plans/llmflows-run-2.md, then build it.

Scanner: flag 0.x-only keyword arguments (engine=, deployment_id=, api_base=, api_type=, api_version=, organization= as per-call kwargs, and others the 0.x API took per call) in calls to openai methods, including when the method is passed as func= / async_func= to a wrapper and the kwargs go through the wrapper call. Suggestion: engine=X / deployment_id=X → model=X; per-call config → the client constructor.
Completeness after the fix: these findings must also be reported if they remain on unchanged lines, so a fix that leaves them is not verified.
Review by hand: when verification is weak, also list remaining findings on unchanged lines, not only changed lines.
Replay llmflows-gptoss-2’s fix: azure_openai.py lines 65 and 89 must now be findings, and the verdict must not be verified_weak.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

- llmflows calls the API through `call_with_retry(func, exceptions_to_retry, max_retries,
  *args, **kwargs)` / `async_call_with_retry(async_func, ...)` (llm_utils.py), which call
  `func(*args, **kwargs)`. The original passes `func=openai.Completion.create` plus
  `engine=self._engine` (azure_openai.py:127, 161), `engine=self._deployment`
  (azure_openai_chat.py:128, 163) and `engine=self.model` (openai_embeddings.py:85, 126).
- The scanner checks per-call kwargs (`rules.KWARG_CHANGES`) only on a direct 0.x resource
  call (`openai.Completion.create(engine=...)`). A method passed as an argument is reported as
  an api_call, but the wrapper call's kwargs are not looked at, so none of the six `engine=`
  lines was a finding.
- The fix rewrote the four other files' kwargs (the model saw them in the call statement) but
  left azure_openai.py's two `engine=self._engine` lines unchanged: fix.diff lines 65 and 89,
  i.e. the fixed file's lines 132 and 172, passed with `func=client.completions.create` on a
  local `client = AzureOpenAI(...)`. The verifier's completeness check rescans the fixed tree,
  but the scanner does not look at 1.x calls at all, so nothing remained; the tests mock
  `call_with_retry`, and the verdict was verified_weak on coverage alone.

## Plan

1. **Per-call kwargs on openai methods** (`python_checks.py`, [certain] `api_call`, on the
   keyword): a call's keywords in `rules.KWARG_CHANGES` (engine, deployment_id,
   request_timeout, headers, api_key, organization, api_base, api_type, api_version; new:
   `request_id`, which 0.x's `create` took as well) are flagged when
   - the call itself is an openai method: a 0.x resource method (as before), or a 1.x method
     (new, so a fix that leaves one is incomplete): `openai.<resource>...<method>` (the
     module-level client) or `<client>.<resource>...<method>`, where `<resource>` is one of
     `rules.CLIENT_RESOURCES` and `<client>` is a client: a constructor call of a client class
     (`OpenAI`, `AsyncOpenAI`, `AzureOpenAI`, `AsyncAzureOpenAI`, and 1.x's `Client` /
     `AsyncClient` aliases; resolved through openai imports), its `.with_options(...)` /
     `.copy(...)`, a name assigned one (`=`, annotated, `:=`, `with ... as`; in the function or
     an enclosing one, or at module level), or `self.x` / `cls.x` assigned one in any method of
     the class (or `x = ...` in the class body). Flow-insensitive, like the unrelated-edit
     check. An unresolved root (a parameter `client`) is not a client: other SDKs share the
     method names.
   - or an openai method (either form, not called) is one of its arguments, positional or
     keyword (`call_with_retry(func=client.completions.create, engine=...)`,
     `partial(openai.Completion.create, engine=...)`, `asyncio.to_thread(...)`): the kwargs go
     through the wrapper. Not flagged when the wrapper is a function defined at the top level
     of the same file that names that parameter itself (it is the wrapper's own); wrappers
     defined in other modules are not read.
   Suggestions with the call's own value: `engine=X` / `deployment_id=X` -> "1.x methods take
   no engine=: pass model=X" (Azure: the deployment name), plus "this call also passes
   model=..., keep one" when it does; `request_timeout=X` -> `timeout=X`; `headers=X` ->
   `extra_headers=X`; `api_key` / `organization` -> the client constructor
   (`openai.OpenAI(api_key=X)`, or `client.with_options(...)`); `api_base` -> `base_url=X`
   on the client (Azure: `azure_endpoint`); `api_type` -> the client class; `api_version` ->
   `openai.AzureOpenAI(api_version=X)`; `request_id` -> drop it. Through a wrapper the
   message starts "`engine=` goes through call_with_retry(...) to client.completions.create:".
2. **Completeness**: nothing new in the verifier: it rescans the whole fixed tree, so a [certain]
   kwarg finding left anywhere (changed or unchanged line) is `incomplete_migration` (reject).
3. **Review by hand** (`verified_weak` / `unverified`): besides the hunks, every remaining
   finding of the fixed tree (only [likely] ones can remain in these verdicts), with whether
   the fix changed its line: `Verification.review_findings` (file, line, category,
   confidence, old_pattern, suggestion, changed), report "Migration findings still in the
   code:" with "(line not changed by the fix)" on unchanged lines, at most 15.
4. Synthetic fixtures: a wrapper module (sync / async wrapper, func= and positional, 0.x and
   1.x, module-level client, `self.client`, a local wrapper with its own `api_key` parameter,
   an unresolved client), scanner tests, verifier tests (1.x call with engine= on an unchanged
   line rejects; review lists remaining likely findings). Replay: `apifix verify` of the run's
   fix.diff on ~/apifix-targets/llmflows (Docker).

## Built

As planned (items 1-4). Notes:

- Kwarg findings are no "call site" for the repo-level pass (`repo_findings.adjust_findings`):
  without that, a fixed tree's leftover `engine=` made every "Never drop" assignment in other
  files say "The API calls are in llmflows/llms/azure_openai.py". Every KWARG_CHANGES message
  starts with "1.x methods take no " (`rules.KWARG_MARK`).
- In a file that sets `api_type = "azure"`, an `engine=` next to `model=` says "on Azure model=
  takes the deployment, so model=X replaces it"; elsewhere "keep one".
- Not checked: mock call assertions (`create.assert_called_once_with(engine=...)` in
  llmflows' tests/llms/test_openai_embeddings.py); the code's own `engine=` is flagged, and a
  test that still asserts it fails against the fixed code.

Scan of llmflows' original: all six `engine=` lines are findings now (azure_openai.py:127, 161;
azure_openai_chat.py:128, 163; openai_embeddings.py:85, 126), each "goes through
(async_)call_with_retry(...) to openai.<Resource>.<method>".

Replay (`apifix verify ~/apifix-targets/llmflows --migration openai-v1 --diff
~/apifix-runs/llmflows-gptoss-2/fix.diff`, Docker): **rejected** (was verified_weak):
`incomplete_migration` at llmflows/llms/azure_openai.py:132 and :172 (fix.diff lines 65 and 89,
`engine=self._engine` passed to `client.completions.create` through call_with_retry /
async_call_with_retry), plus the earlier `low_changed_line_coverage`. Migration findings: 84 of
90 resolved, 2 certain remaining.
