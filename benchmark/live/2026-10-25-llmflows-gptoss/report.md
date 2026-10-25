# apifix fix: Verified, with weak evidence

- Migration: `openai-v1` (OpenAI Python library 0.x -> 1.x)
- Outcome: verified_weak on attempt 1
- Attempts: 1 (max 4 that reach verification, 8 in all; 0 ended at the checks and did not count); changes from attempt 1
- Model: `openai-compat` / `openai/gpt-oss-120b`
- Edit mode: whole-file
- Tokens: 51,095 (8,185 of the output tokens reasoning, as the server reported); cost: $0.00
- Time: 411 s total; LLM 295 s (8 responses); sandbox 108 s (baseline and 1 verification)
- apifix set: pyproject.toml:22 openai==0.28.1 -> openai>=1.55.3,<3
- apifix set: requirements.txt:1 openai==0.28.1 -> openai>=1.55.3,<3
- Dependency downloads (pip cache on): baseline: 0 files downloaded (0.0 MB), 88 from the pip cache (68.4 MB); attempt 1: 0 files downloaded (0.0 MB), 82 from the pip cache (67.6 MB)
- Dependency sources conflict: `pyproject.toml` pins pinecone-client==2.2.4, tiktoken==0.5.1 where `requirements.txt` pins pinecone-client==2.2.1, tiktoken==0.3.3: installed from `requirements.txt` (the repo's CI installs it: .github/workflows/pylint.yml), not `pyproject.toml`
- apifix reverted: llmflows/llms/openai.py:57 (text added outside the migration: "# Keep the API key on the module so that the module‑level client can read it." removed; attempt 1)
- apifix reverted: llmflows/llms/openai_embeddings.py:8 (blank lines added outside the migration, removed; attempt 1)
- apifix reverted: llmflows/llms/openai_embeddings.py:16 (blank lines added outside the migration, removed; attempt 1)
- apifix reverted: tests/llms/test_openai.py:50 (text changed outside the migration: "...# Check that openai.completions.create was called with t..." restored to the original; attempt 1)
- apifix reverted: tests/llms/test_openai_chat.py:44 (blank lines removed outside the migration, put back; attempt 1)
- apifix reverted: tests/llms/test_openai_chat.py:70 (text changed outside the migration: "...# Check that openai.chat.completions.create was called with t..." restored to the original; attempt 1)
- Region edits: `tests/llms/test_openai.py` (2,935 bytes, the server refused it as a whole file): R1 TestOpenAI.test_generate (lines 39-60), R2 TestOpenAI.test_format_results (lines 66-83); 66% of the file sent
- Token limit known (8,000): requests from then on got only their own file's guide parts, shorter test output, compact context, and regions or batches where a whole file would not fit
- Guide sections sent: client, client.async, client.azure, client.kwargs, client.config, resources, responses, errors, tests
- Attempt 1 (verified_weak): `llmflows/llms/azure_openai.py` ok, `llmflows/llms/azure_openai_chat.py` ok, `llmflows/llms/openai.py` ok, `llmflows/llms/openai_chat.py` ok, `llmflows/llms/openai_embeddings.py` ok, `tests/llms/test_openai.py` failed: provider_error: HTTP 413 from https://api.example.com/openai/v1: Request too large for model `openai/gpt-oss-120b` in organization `org_<redacted>` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 9421, please reduce your message size and try again. Need mor..., `tests/llms/test_openai.py` ok, `tests/llms/test_openai_chat.py` ok, `tests/llms/test_openai_embeddings.py` ok; kept: `pyproject.toml`, `requirements.txt`
- Note: repository code is sent to the third-party server https://api.example.com/openai/v1
- Note: 1 request(s) refused by the server's per-minute token limit (8,000) were sent again with fewer reply tokens (max_tokens lowered to fit)
- Note: rate limited by the server 8 time(s); apifix waited 230 s in total and retried (request durations include the waits)

## Imported but not declared

Found by reading the code before any sandbox run. Information only: nothing was installed because of this list. To install them in the sandbox, list them in a `--sandbox-requirements` file.

- `mediawiki` (pip: `mediawiki`): examples/react_agent/tools.py

## Verification: Verified, with weak evidence

| | Original code | Fixed code |
|---|---|---|
| Test run | failed | failed |
| Passed / failed / errors / skipped | 46 / 1 / 0 / 0 | 46 / 1 / 0 / 0 |
| Python / image | 3.11 / python:3.11-slim | 3.11 / python:3.11-slim |

**Changed-line coverage:** tests executed 8 of 22 changed source lines (36%).
**Dependency smoke check** (`import openai; openai.OpenAI(api_key="<dummy key>")`): passed.
**Dependency sources:** `pyproject.toml` pins pinecone-client==2.2.4, tiktoken==0.5.1 where `requirements.txt` pins pinecone-client==2.2.1, tiktoken==0.3.3: installed from `requirements.txt` (the repo's CI installs it: .github/workflows/pylint.yml), not `pyproject.toml`.
**Migration findings:** 87 of 90 resolved, 0 certain remaining.

### Why this verdict

- **low_changed_line_coverage**: tests executed 8 of 22 changed lines (36%, threshold 50%); not executed: llmflows/llms/azure_openai.py:73,88,119,159; llmflows/llms/azure_openai_chat.py:75,92,118,158; llmflows/llms/openai.py:137; llmflows/llms/openai_chat.py:141,142,143; llmflows/llms/openai_embeddings.py:118,135

### Review by hand

No test executed these changes:

- `llmflows/llms/azure_openai.py:11`: `Timeout,` -> `APITimeoutError,`
- `llmflows/llms/azure_openai.py:14-16`: `ServiceUnavailableError,` -> `InternalServerError,` (+2 lines)
- `llmflows/llms/azure_openai.py:73`: `openai.api_key = self._api_key` -> `self._azure_api_version = azure_api_version`
- `llmflows/llms/azure_openai.py:87-88`: `text_result = model_outputs.choices[0]["text"]` -> `text_result = model_outputs.choices[0].text` (+1 line)
- `llmflows/llms/azure_openai.py:118-124`: added `client = AzureOpenAI(` (+6 lines)
- `llmflows/llms/azure_openai.py:126`: `func=openai.Completion.create,` -> `func=client.completions.create,`
- `llmflows/llms/azure_openai.py:129`: `Timeout,` -> `APITimeoutError,`
- `llmflows/llms/azure_openai.py:132`: `ServiceUnavailableError,` -> `InternalServerError,`
- `llmflows/llms/azure_openai.py:134`: removed `engine=self._engine,`
- `llmflows/llms/azure_openai.py:135`: `model=self.model,` -> `model=self._engine,`
- `llmflows/llms/azure_openai.py:158-164`: added `async_client = AsyncAzureOpenAI(` (+6 lines)
- `llmflows/llms/azure_openai.py:166`: `async_func=openai.Completion.acreate,` -> `async_func=async_client.completions.create,`
- `llmflows/llms/azure_openai.py:169`: `Timeout,` -> `APITimeoutError,`
- `llmflows/llms/azure_openai.py:172`: `ServiceUnavailableError,` -> `InternalServerError,`
- `llmflows/llms/azure_openai.py:174`: removed `engine=self._engine,`
- ... and 47 more (see fix.diff)

Migration findings still in the code:

- `llmflows/llms/openai.py:57` (line not changed by the fix): client_config [likely] `openai.api_key = self._api_key` -> Never drop this value: keep the assignment and call the module-level client (openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: openai.OpenAI(api_key=self._api_key)
- `llmflows/llms/openai_chat.py:64` (line not changed by the fix): client_config [likely] `openai.api_key = self._api_key` -> Never drop this value: keep the assignment and call the module-level client (openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: openai.OpenAI(api_key=self._api_key)
- `llmflows/llms/openai_embeddings.py:52` (line not changed by the fix): client_config [likely] `openai.api_key = self._api_key` -> Never drop this value: keep the assignment and call the module-level client (openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: openai.OpenAI(api_key=self._api_key)

### Declared dependencies not installed

- `pyproject.toml`: aiohttp: not installed (its pins conflict with requirements.txt, which was installed instead)

### Tests that give no signal

These fail with and without the fix, so they are not counted as passes.

- `tests.llms.test_claude_chat.TestClaudeChat::test_generate`: already failing before the fix: `TypeError: Client.__init__() got an unexpected keyword argument 'proxies'`

### For reviewers

- info: `tests/llms/test_openai.py:44` setup_changed: test body changed
- info: `tests/llms/test_openai.py:70` setup_changed: test body changed
- info: `tests/llms/test_openai_chat.py:34` setup_changed: TestOpenAIChat.tes_format_results changed
- info: `tests/llms/test_openai_chat.py:59` setup_changed: test body changed
- info: `tests/llms/test_openai_embeddings.py:26` setup_changed: test body changed
- info: `tests/llms/test_openai_embeddings.py:49` setup_changed: test body changed
- info: `llmflows/llms/openai.py:57` remaining_likely_finding: client_config: `openai.api_key = self._api_key` -> Never drop this value: keep the assignment and call the module-level client (openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: openai.OpenAI(api_key=self._api_key)
- info: `llmflows/llms/openai_chat.py:64` remaining_likely_finding: client_config: `openai.api_key = self._api_key` -> Never drop this value: keep the assignment and call the module-level client (openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: openai.OpenAI(api_key=self._api_key)
- info: `llmflows/llms/openai_embeddings.py:52` remaining_likely_finding: client_config: `openai.api_key = self._api_key` -> Never drop this value: keep the assignment and call the module-level client (openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: openai.OpenAI(api_key=self._api_key)
