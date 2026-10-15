# Plan: automatic imports, object-shaped mock data, errors carried into retries

Requested 2026-10-15, to be built directly (no separate review). The request is verbatim.

## Request

Context: Read CLAUDE.md and docs/plans/mock-guidance.md. Live run 3 (trajectory at
~/apifix-runs/wholefile-7b-3.json) was rejected, but it’s close. In attempts 1 and 2, app.py
alternated between missing import os and missing from openai import OpenAI, using up its one
retry each time. In attempt 3, app.py and the mock target were right (patch("app.OpenAI") with
the return_value chain), but the test helper completion() still returns a dict while app.py now
uses attribute access, so both tests failed. The model also added os.getenv("OPENAI_API_KEY"),
which was flagged as a suspicious import.

Task: record this request verbatim in a new plan file, docs/plans/imports-and-mock-data.md, then
build it. If anything conflicts with the code, stop and ask.

Add missing imports automatically. When a candidate has an undefined name with exactly one known
import (the stdlib table already used in the hint messages, plus migration-specific names such as
OpenAI → from openai import OpenAI), apifix inserts the import itself, after the existing
imports, before the checks run. Record each one in the trajectory and the report (“apifix added:
from openai import OpenAI”). Names with no known import, or more than one, still fail the check
as now.
Update the openai-v1 guide:
OpenAI() reads OPENAI_API_KEY from the environment, so don’t add os.getenv or import os for it.
In tests, mock responses must be objects, not dicts, because 1.x code uses attribute access.
Helpers like completion() that build dict responses must return objects, e.g. with
SimpleNamespace. Show a short example.
Show the response shape in the retry example. Replace <the test's mock response> with a real
object-shaped response built with SimpleNamespace, and mention that dict-returning helpers must
change.
New scanner rule for the openai-v1 migration: in test files, flag dict literals shaped like an
API response (a "choices" key containing "message" or "text") as a [likely] finding: “1.x
responses are objects: build mock responses with attribute access (e.g. SimpleNamespace), not
dicts.” The model then sees it in the test file’s prompt.
Carry earlier errors into retries. In a file’s retry message, also list problems fixed in earlier
replies for that file in this job, as things to keep fixed, so a fix for one problem doesn’t undo
another.

Tests: replay run 3’s files (the oscillating app.py replies should now pass after auto-imports;
the attempt-3 test file should get the new finding and the new example), each auto-import case
(stdlib, migration name, ambiguous, unknown), and the scanner rule (helpers, inline dicts,
non-test files ignored). Run the full suite with --docker and ruff, then commit. Don’t run the
live tests.

## What run 3 shows

- **app.py oscillated** between two replies:
  - `from openai import OpenAI` with `os` undefined;
  - `import openai` / `import os` with `OpenAI` undefined.

  Each time, the one check retry fixed the reported name and dropped the other. Both
  names have exactly one obvious import.
- **The guide itself prompts `os.getenv`.** The Client section's 1.x example is
  `OpenAI(api_key=os.getenv("OPENAI_API_KEY"), ...)`. The model copied it, and the verifier
  warned about the new `import os`.
- **Attempt 3:** the mock target and chain were right. But the unchanged helper `completion()`
  still returns `{"choices": [...]}`, so `response.choices` failed in both tests. Nothing told
  the model about the helper: the test file's findings were only the two string targets.

## Plan

### 1. Automatic imports (`code_checks.py`, `whole_file.py`)

- `import_table(*tables) -> dict[str, tuple[str, ...]]`: merges name → import tables into
  name → the distinct imports offered. The tables are the existing `STDLIB_IMPORTS` and a new
  `Migration.known_imports`.
- `add_missing_imports(path, original, candidate, known) -> (text, added)`:
  - Takes the names undefined in the candidate and not in the original (the same rule as the
    undefined-name check). Each name with exactly one known import gets that import line.
  - Lines are inserted after the module's leading imports: after the docstring, then the
    contiguous run of `import` / `from` statements at the top, multi-line imports included.
    With no leading imports, they go after the docstring. The file's line endings are kept.
  - Nothing happens when the candidate doesn't parse.
- `check_candidate(..., known_imports=...)` runs this first, for Python files whose syntax
  check passes, so every later check sees the completed file. `CheckResult` gets `text` (what
  is written) and `auto_imports` (the lines added).
- The undefined-name message keeps its hint for a single import, and lists `(add one of: ...)`
  for an ambiguous name. With auto-imports on, a single-import name never reaches the message.
- openai-v1 `known_imports` (pure data in `rules.py`):
  - `OpenAI`, `AsyncOpenAI`, `AzureOpenAI`, `AsyncAzureOpenAI`;
  - the 1.x error classes (from `ERROR_CLASSES` plus `APIStatusError`, `NotFoundError`,
    `UnprocessableEntityError`, `ConflictError`) → `from openai import X`;
  - `openai` → `import openai`; `httpx` → `import httpx`.
- Trajectory: each reply records `auto_imports`, and so does each file entry (from the
  accepted reply).
- Report: one line per accepted file that got imports, e.g. `- apifix added: from openai
  import OpenAI (app.py, attempt 1)`.
- The suspicious-edit check still runs on the written file. An `import os` that apifix adds
  because the model used `os` is warned about like any other new import; the report says who
  added it.

### 2. Guide (`guide.md`)

- **Client part** (always sent):
  - the 1.x example drops `api_key=os.getenv("OPENAI_API_KEY")`, with a comment that
    `OpenAI()` reads `OPENAI_API_KEY` itself;
  - the bullet says not to add `os.getenv` or `import os` for it.
- **Tests part:** mock responses must be objects, not dicts. Helpers such as `completion()`
  that build dict responses must return objects. A 0.x → 1.x helper example with
  `SimpleNamespace` goes before the patterns.

### 3. Response shape in the retry example (`code_checks.py`)

- `Migration.mock_responses` maps a call path to an object-shaped example (openai-v1):
  - `chat.completions.create` → `SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))])`
  - `completions.create`, `embeddings.create`, `moderations.create`, `images.generate`,
    `audio.transcriptions.create` likewise.
- The suggestion knows its call path, and the longest matching key decides. The example's last
  line becomes `<chain> = SimpleNamespace(`, then the inner part, then `)` (5 lines in all).
- With no matching shape, the `<the test's mock response>` placeholder stays.
- With a matching shape, the message adds one sentence: "Mock responses must be objects, not
  dicts". If the candidate test file has top-level functions that `return` a dict literal, they
  are named ("completion() returns a dict: make it return an object, e.g. with
  SimpleNamespace").
- `mock_target_problems(..., mock_responses=...)`: whole-file passes the migration's table.

### 4. Scanner rule (`openai_v1/python_checks.py`)

- **What it flags:** in test files (`tampering.is_test_file`), a dict literal with a constant
  key `"choices"` whose value is a list or tuple holding a dict literal with a `"message"` or
  `"text"` key.
- **The finding:** category `response_access`, [likely]; suggestion `rules.MOCK_DICT_RESPONSE`
  = the requested text. Only the outer dict is reported. The rule does not require an openai
  import (test files mock by string).
- **Not covered:** `dict(choices=...)` calls, dicts built up step by step, and non-test files.
- **Effects:**
  - the test file's whole-file prompt lists the finding;
  - a fix that leaves the dict keeps a [likely] finding, so the verifier gets an info reason
    (`remaining_likely_finding`), not a rejection;
  - scope warnings for edits to that test file go away, because the file now has findings.

### 5. Earlier errors in retries (`agent.py`, `whole_file.py`)

- The job keeps, per file, every check error of every earlier reply, in order.
  `check_retry_message(path, error, keep_fixed=[...])` adds:
  `Keep these fixed (problems in your earlier replies for this file):` followed by a bullet
  list.
- The list holds earlier errors that are not among the current ones. Errors are compared
  without the `line N: ` prefix, and only each error's first line is shown (no examples).
- Whole-file only, like the other checks.

### Tests

- `test_code_checks.py`:
  - auto-imports: stdlib (`SimpleNamespace`, `os`), migration name (`OpenAI`), ambiguous (a
    test table with two imports for one name), unknown;
  - insertion after leading imports, after a docstring, with no imports, after a multi-line
    import, with CRLF;
  - names already undefined in the original are not touched;
  - the response-shape example and the dict-helper sentence;
  - the run-3 attempt-3 test file against the run-3 app.py, verbatim.
- `test_whole_file.py`: `check_candidate` with known imports (text, `auto_imports`, checks);
  `check_retry_message` with `keep_fixed`.
- `test_scanner.py` / `test_migrations.py`:
  - the scanner rule: a helper's `return`, an inline assignment, `"text"` completions, a dict
    without `"message"`/`"text"`, a non-test file, the tiny repo's test file;
  - `known_imports` covers every 1.x error class;
  - the guide's Client example has no `os.getenv`.
- `test_agent.py`:
  - replay of run 3: the oscillating app.py replies pass in their first reply, with the
    import recorded and reported;
  - the attempt-3 test file is rejected with the new example, and its prompt lists the new
    finding;
  - a fixed test file passes (also run for real against the live app.py with an openai stub);
  - `keep_fixed` reaches a retry message.

The full suite with `--docker` plus ruff passes before the commit. Live tests are not run.

## As built (2026-10-15)

As planned, with these details.

- **Auto-imports:**
  - One line per name, never merged into an existing `from` line; several new names give
    several lines, in line order of first use.
  - A last import line without a newline gets one before the inserted lines.
- **Retry message layout:** with `keep_fixed`, the message always uses the multi-line form
  (rejected / errors / keep-fixed list / return instruction). Each nudge records
  `keep_fixed`.
- **Run 3's replies are test fixtures:** `tests/fixtures/live_run3/*.txt`, verbatim. They are
  `.txt` because they are model output with deliberate bugs that ruff would flag.
- **Older replays changed:** run 1's missing `SimpleNamespace` import is now added by apifix,
  so its replay sees only the mock-target error. Its whole-file messages now carry the
  object-shaped example and "completion() returns a dict" (`LIVE_TARGET_WF`,
  `LIVE2_CODE_UNDER_TEST_WF`).
- **Guide:** the Client part's 1.x example no longer passes `api_key=os.getenv(...)`. It was
  the likely source of the model's `os.getenv`.
- **Checked by running:** the run-3 test file with its dict helper fails against the run-3
  app.py (`'dict' object has no attribute 'choices'`), and the object helper passes. Both run
  under pytest with an openai stub.
