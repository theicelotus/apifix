# apifix fix: Verified, with weak evidence

- Migration: `openai-v1` (OpenAI Python library 0.x -> 1.x)
- apifix: 0.1.0 (git commit e2d9de215adb)
- Outcome: verified_weak on attempt 1
- Attempts: 1 (max 4 that reach verification, 8 in all; 0 ended at the checks and did not count); changes from attempt 1
- Model: `openai-compat` / `openai/gpt-oss-120b`
- Edit mode: whole-file
- Tokens: 10,544 (1,541 of the output tokens reasoning, as the server reported); cost: $0.00
- Time: 104 s total; LLM 37 s (2 responses); sandbox 65 s (baseline and 1 verification)
- apifix set: pyproject.toml:11 openai = "^0.27.6" -> openai = ">=1.55.3,<3"
- apifix set: requirements.txt:1 openai -> openai>=1.55.3,<3
- not updated: poetry.lock; regenerate it
- Dependency downloads (pip cache on): baseline: 0 files downloaded (0.0 MB), 50 from the pip cache (81.4 MB); attempt 1: 0 files downloaded (0.0 MB), 56 from the pip cache (85.1 MB)
- Guide sections sent: client, client.kwargs, client.config, resources, responses
- Attempt 1 (verified_weak): `aipl/clients.py` ok, `aipl/ops/llm.py` ok; kept: `pyproject.toml`, `requirements.txt`
- Note: repository code is sent to the third-party server https://api.example.com/openai/v1
- Note: rate limited by the server 2 time(s); apifix waited 26 s in total and retried (request durations include the waits)

## Imported but not declared

Found by reading the code before any sandbox run. Information only: nothing was installed because of this list. To install them in the sandbox, list them in a `--sandbox-requirements` file.

- `requests` (pip: `requests`): aipl/clients.py
- `visidata` (pip: `visidata`): aipl/ops/debug.py

## Verification: Verified, with weak evidence

| | Original code | Fixed code |
|---|---|---|
| Test run | failed | failed |
| Passed / failed / errors / skipped | 53 / 2 / 0 / 0 | 53 / 2 / 0 / 0 |
| Python / image | 3.11 / python:3.11-slim | 3.11 / python:3.11-slim |

**Changed-line coverage:** tests executed 0 of 10 changed source lines (0%).
**Dependency smoke check** (`import openai; openai.OpenAI(api_key="<dummy key>")`): passed.
**Migration findings:** 11 of 11 resolved, 0 certain remaining.

### Why this verdict

- **low_changed_line_coverage**: tests executed 0 of 10 changed lines (0%, threshold 50%); not executed: aipl/clients.py:60,61,62,63,108,113,128,174; aipl/ops/llm.py:74,76

### Review by hand

No test executed these changes:

- `aipl/clients.py:60-63`: `used = resp['usage']['total_tokens']` -> `usage = getattr(resp, "usage", None)` (+3 lines)
- `aipl/clients.py:108`: `resp = openai.ChatCompletion.create(` -> `resp = openai.chat.completions.create(`
- `aipl/clients.py:113`: `result = resp['choices'][0]['message']['content']` -> `result = resp.choices[0].message.content`
- `aipl/clients.py:128`: `openai.api_base = "https://api.goose.ai/v1"` -> `openai.base_url = "https://api.goose.ai/v1"`
- `aipl/clients.py:174`: `openai.api_base = os.environ['LLM_CLIENT_ENDPOINT']` -> `openai.base_url = os.environ['LLM_CLIENT_ENDPOINT']`
- `aipl/ops/llm.py:74`: `resp = openai.Embedding.create(input=v, **kwargs)` -> `resp = openai.embeddings.create(input=v, **kwargs)`
- `aipl/ops/llm.py:76`: `used = resp['usage']['total_tokens']` -> `used = resp.usage.total_tokens if resp.usage else None`
- `aipl/ops/llm.py:81`: `embedding=resp['data'][0]['embedding'])` -> `embedding=resp.data[0].embedding)`

### Tests that give no signal

These fail with and without the fix, so they are not counted as passes.

- `tests.test_scripts::test_script[tests/test-sh-stdin.aipl]`: already failing before the fix: `aipl.AIPLException: [line 5] no such operator "!assert_equal"`
- `tests.test_split_unicode::test_split_unicode`: already failing before the fix: `AssertionError: assert 1 == 4`
