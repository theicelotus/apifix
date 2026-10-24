# Mock-target suggestions from library-using modules, repeated check errors

Requested 2026-10-23. The request is verbatim. Built in the same step (no review stop was
asked for).

## Request

Context: Read CLAUDE.md. genai run ~/apifix-runs/genai-gptoss-2/ (~/apifix-targets/genai): region edits worked, but attempts 2–4 all failed the mock-target check with the same error: the model patched genai.context.OpenAI, and genai/context.py doesn’t use openai. The openai code is in genai/generate.py, which tests/test_context.py reaches only through IPython magics. The message names the problem but not the right target, so the model repeated the mistake 3 times.

Task: record this request verbatim in docs/plans/mock-target-suggestions.md, then build it.

Suggest the right target when the patched module has no library use: list the repo’s non-test modules that use the library and, for each, the concrete target and mock chain that fits how it binds the name: from openai import OpenAI → patch("genai.generate.OpenAI") with .return_value.chat.completions.create.return_value; import openai + openai.OpenAI() → patch("openai.OpenAI"); module-level client calls → patch("openai.chat.completions.create"); a module-level client = OpenAI() → patch("genai.generate.client.chat.completions.create"). Prefer modules the test reaches (imports or the context’s reachability), and fall back to all library-using modules, capped at 3. Include a 3–5 line example as the other messages do. Use the post-fix state of each module (files accepted earlier in the attempt).
Repeated identical check errors: when the same file fails the same check with the same error key on two consecutive attempts, the next request for it should say so explicitly (“this is the same error as last time; your previous fix did not change the target”), and if it happens a third time, stop retrying that file and end the job with a clear reason instead of spending the remaining attempts.
Look at attempt 1’s rejection of genai/generate.py (an “added code unrelated to the migration” hunk shown as """"") and tell me whether it was a correct rejection or a false positive.

Synthetic fixtures modelled on genai’s pattern (a test reaching the openai module only indirectly). Fast suite, Docker if the network allows, and ruff, then commit.

## Findings

### Attempts 2–4: `genai.context.OpenAI`

- Attempt 2 accepted `genai/generate.py` (`from openai import OpenAI`, `client = OpenAI()`
  inside both functions, `client.chat.completions.create(...)`); it was kept for attempts 3
  and 4. So when `tests/test_context.py` was checked, the working copy already had the 1.x
  module.
- Every reply for the test (attempts 2, 3, 4, each with its check retry: six replies) patched
  `genai.context.OpenAI`. `_missing_name_problem` found no `OpenAI` in `genai/context.py` and
  `where_created("OpenAI")` found nothing (context.py never touches openai), so the message
  ended with the generic "Patch a name that exists in genai/context.py".
- The right target is `genai.generate.OpenAI` with
  `OpenAI.return_value.chat.completions.create.return_value`. The test reaches generate.py
  through `from genai.context import ...`, which imports the `genai` package, whose
  `__init__.py` imports `genai.magics`, which imports `genai.generate`
  (`context.reachable_modules` follows exactly this chain).
- The same replies also dropped the `"role": "assistant"` strings from the mock responses (an
  unrelated-edit error each time), so the error key set per attempt was {mock target, missing
  string}.

### Attempt 1: the `"""""` hunk in genai/generate.py

Reproduced offline with `unrelated_edits` on the reply. The model rewrote `content()` and
`deltas()` to accept both 0.x dicts and 1.x objects, and gave both a new docstring. In
`content()` the docstring sits in the same diff hunk as the changed `return` (which uses the
openai-bound parameter `completion`), so it was kept. In `deltas()` the unchanged line
`for chunk in completion:` splits the docstring from the body change, so it is a hunk of its
own: an inserted string statement (a code hunk, not text-only: a docstring is a STRING token),
11 lines from the nearest finding, using no bound name. Hence "after original line 42: added
code unrelated to the migration", and the message shows its first line, `"""`, inside its own
quotes: `("""""`.

Verdict: a false positive in substance, though correct by the letter of the rule. The rule
says a code hunk must be near a finding or touch a library-bound name, and a lone docstring
does neither. But the docstring documents a body change that the same check accepted as
related (the next hunk uses `chunk`, bound through `deltas(response)`), and the identical
edit in `content()` passed. Whether it was rejected depended only on how difflib aligned the
lines. The rejection did no harm (attempt 2's version was cleaner, without the dead dict
branch), but it cost an attempt. Not changed in this step; a possible fix is listed under
"Later".

Unrelated to the checks: both accepted versions of `deltas()` use
`if hasattr(delta, "content"): yield delta.content`. In 1.x `delta.content` always exists and
is `None` on the final chunk, so streaming now yields a trailing `None` where 0.x's
`if "content" in delta` skipped the empty delta. No test streams, so the verifier cannot see
it.

## Plan

### 1. Suggestions from library-using modules (`code_checks.py`)

When a new mock target is wrong and the message has no suggestion from the patched module
itself, list the modules where a patch would work:

- **When:** `_missing_name_problem` with no `where_created` suggestion, and
  `_code_under_test_problem` with no `what_it_calls` suggestion.
- **Which modules:** the repo's non-test modules (`modules`) whose code reaches the library
  through a recognisable call (below), in their current working-copy text (files accepted so
  far, the candidate standing in for the test). Left out: modules the job has not migrated
  yet (`unmigrated_modules`: non-test files with findings that no reply has been accepted
  for, e.g. skipped files), since their 0.x calls are not targets for 1.x code. The patched
  module itself comes first when it has such a use. Then the modules the test reaches
  (`context.reachable_modules` from the test file: imports, packages' `__init__.py`,
  relative imports), in the order found; when it reaches none, all of them in repo order.
  At most 3; more are named as "(also: a, b)".
- **The target, by how the module binds the name** (the first library call in its top-level
  functions, source order, through `_ModuleIndex.what_it_calls`):
  - `from openai import OpenAI` + `client = OpenAI()` in a function:
    `patch("genai.generate.OpenAI")`, chain `OpenAI.return_value.chat.completions.create.return_value`;
  - `import openai` + `openai.OpenAI()`: `patch("openai.OpenAI")`, same chain (the module's
    `openai` name is the library itself, so the library path is the target);
  - module-level client calls `openai.chat.completions.create(...)`:
    `patch("openai.chat.completions.create")`, chain `create.return_value`;
  - a module-level `client = OpenAI()`: `patch("genai.generate.client.chat.completions.create")`,
    chain `create.return_value`;
  - a factory (`get_client()` returning a client): `patch("<module>.get_client")` as today.
  The module's dotted name is the one the tests import it by (`_RepoImports.module_path`).
- **Message:** the first line keeps its current head and says why
  ("..., and genai/context.py does not use openai. Patch openai where a module the test
  reaches uses it:"), then one indented line per module (`genai/generate.py:
  generate_next_from_history() creates client = OpenAI(): patch("genai.generate.OpenAI") and
  set OpenAI.return_value.chat.completions.create.return_value.`), the objects-not-dicts note
  as today, and a 3–5 line example for the first module in the test's own patch form
  (decorator, `with`/call, or `mocker.patch`), with the object-shaped response.
- `mock_target_problems` gets `path` (the test file, for reachability) and
  `unmigrated_modules`; `check_candidate` passes them through. `attribute_errors` already
  sends a mock-target error to the regions quoting the target.

### 2. Repeated identical check errors (`agent.py`, `whole_file.py`)

- `CheckResult.error_checks`: the check name of each error (`syntax`, `mock_targets`,
  `unrelated_edits`, ...; splice errors from region edits: `regions`).
- Each whole-file entry records `failed_checks` (`[check, error_key]` pairs of its last
  reply, when that reply failed a check). After an attempt, per file and pair, a streak of
  consecutive attempts the file failed with that pair; acceptance, or a failure without the
  pair (also a reply without a code block), ends it.
- **Two in a row:** every request for that file in the next attempt (the first request, and
  a check retry whose errors still include the pair) says, after the previous-version diff:
  "This is the same error as last time (attempts 2 and 3): <key>. Your previous fix did not
  change the target; use the target the error names." For other checks the second sentence is
  "Your previous fix did not change it; do something different." The entry records
  `repeated_errors` (check, key, attempts).
- **Three in a row:** the job stops after that attempt instead of spending the remaining
  ones. Outcome as before (aborted when no attempt was verified, else the best attempt), with
  reason `repeated_check_error: tests/test_context.py failed the mock_targets check with the
  same error on attempts 2, 3 and 4 (<key>); stopped instead of spending the remaining
  attempts` (appended to the best-attempt reason when there is one), and a report line
  "- Stopped early: ...". Constants `REPEATED_ERROR_NOTE_AT = 2`, `REPEATED_ERROR_STOP_AT = 3`.
- Whole-file mode only (agent mode has no per-file checks).

### 3. Tests (synthetic fixtures)

- New fixture `tests/fixtures/indirect_reach/` modelled on genai: package `notebook/` with
  `__init__.py` importing `magics.py`, which imports `generate.py` (openai 0.x calls);
  `context.py` without openai; `tests/test_context.py` importing only `notebook.context` and
  patching `openai.ChatCompletion.create` with a dict response; a second library-using module
  the test does not reach (`batch.py`).
- code_checks: each binding form gives its target and chain; reached modules first, fallback
  to all, cap 3; the patched module's own use first; unmigrated modules left out; the example
  in decorator / `with` / `mocker` form; the suggested patch passes the check; code-under-test
  path gets the list too.
- agent (FakeLLM, whole-file): the fixture with the test repeating `notebook.context.OpenAI`;
  the message names `notebook.generate.OpenAI`; the "same error as last time" note in attempt
  3's request; the job stopping after attempt 3 with the reason and report line, with
  attempts left; a streak broken by a different error does not stop.

### 4. Docs

CLAUDE.md "Known limitations (whole-file mode)" (suggestions, repeated errors) and the layout
line for code_checks.py; README if it describes the retry loop.

## Later

- Docstrings in the unrelated-edit check: treat a string statement inserted as the first
  statement of a function as related when another hunk of the same function is related (or
  as text-only), and show `"""` in messages without the doubled quotes.
- Library uses through methods (`self.client = OpenAI()` in `__init__`, calls in other
  methods) and module-level script code are not suggestion sources yet.
- The example responses (`MOCK_RESPONSES`) build `message=SimpleNamespace(content=...)`
  without `role`. In genai every reply copied that shape, dropped `"role": "assistant"`, and
  failed the unrelated-edit check on the missing `'assistant'` strings as well, on all three
  attempts. Adding `role="assistant"` to the chat example would steer models to keep it.

## Built

As planned, with these differences:

- The example keeps the forms the other messages use (decorator, or `mocker.patch`); a
  `with patch(...)` test gets the decorator form, as before.
- No separate "Stopped early" report line: the report's Outcome line already carries the
  reason (`repeated_check_error: ...`, or the best attempt's reason with "; stopped early:
  ..."), and so does the trajectory's `reason`.
- Streaks count a file's consecutive tries: an attempt that ends before the file is sent
  (an earlier file failed) neither extends nor breaks them, so the note can say
  "attempts 2 and 4".
- Replayed offline on genai-gptoss-2: attempt 2's reply for tests/test_context.py, checked
  against the working copy with attempt 2's generate.py, now gets
  `genai/generate.py: generate_next_from_history() creates client = OpenAI():
  patch("genai.generate.OpenAI") and set
  OpenAI.return_value.chat.completions.create.return_value.` with the example
  (`@patch("genai.generate.OpenAI")`, the test's own name). The two `'assistant'` errors stay.
- Fixture `tests/fixtures/indirect_reach/`; the suggested target also passes the fixture's
  tests under an openai stub, and the wrong one fails them (`does not have the attribute
  'OpenAI'`).
