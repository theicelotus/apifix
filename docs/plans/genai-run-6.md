# genai run 6: last good version on a failed resend, mock failures attributed to tests, stream-chunk role, factory targets, attempt budget

## Request

Context: Read CLAUDE.md and docs/plans/genai-run-5.md. genai run ~/apifix-runs/genai-gptoss-6/ (gpt-oss-120b, reasoning low): attempt 3 reached verification with 24/24 findings resolved and 2 regressions (AttributeError: 'types.SimpleNamespace' object has no attribute 'create' in tests/test_generate.py and tests/test_magics.py: the test’s mock client lacks create). Attempt 4 resent genai/generate.py (its line was in the traceback) instead of the tests, the model’s rewrite reintroduced "content" in delta and failed the streaming check, and all 4 test files were held back because they import it. The job ended. Record this request verbatim in docs/plans/genai-run-6.md, then build it.

Keep the last good version: when a file that was accepted in an earlier attempt fails its checks on a resend, fall back to its last accepted version for this attempt (record it), so files that import it are still sent instead of held back.
Attribute mock failures to the tests: when a regression’s error is an AttributeError (or TypeError) on a mock object (SimpleNamespace, Mock, MagicMock, …) raised inside library-call code, the region to resend is the failing test’s mock setup, not the module whose line is in the traceback. Add a failure hint: “the test’s mock client is incomplete: <obj> has no <attr>; build the client mock with MagicMock() (or include the whole chat.completions.create chain) and set .return_value on create”.
Stream-chunk examples keep the role: the suggestion and examples for mocked stream chunks (MOCK_DICT_CHUNK) should keep role in the delta when the original has it (attempts 1–2 failed twice on “the string ‘assistant’ is missing”).
Wrong module for a factory: attempts 1–2 patched genai.context.get_client / genai.suggestions.get_client after the model added get_client() to genai/generate.py. Check that the mock-target suggestion names genai.generate.get_client for a module-level factory, and that the message isn’t cut off before the suggestion. Replay attempt 1.
Attempt budget: propose whether attempts that never reach verification (all files rejected by checks) should count against the limit, or whether the default limit should scale with the number of files, and build the simpler option.

Replay this run’s attempts 3–4 to confirm the tests would be resent with the hint. Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

From `~/apifix-runs/genai-gptoss-6/trajectory.json` (`openai/gpt-oss-120b` on a hosted endpoint, 8,000 TPM,
`--reasoning-effort low`, 4 attempts):

- **Attempt 4 did not resend generate.py because of a traceback line.** After a rejection every
  Python file is resent (`_after_rejection`), so generate.py went first; its rewrite put
  `"content" in delta` back and failed the streaming check twice, so it went into the
  attempt's set-aside list and all four test files (each imports it) were held back. The
  working copy still had generate.py's attempt-1 version: a failed request never writes it
  (`_accept` only runs on success, region batches too). So the tests were held back against a
  module that was in fact fine.
- **The regressions were the tests' own mock setup.** test_generate.py and test_magics.py built
  the client as `mock_client = SimpleNamespace(); mock_client.chat = SimpleNamespace();
  mock_client.chat.completions = SimpleNamespace()` and then set
  `mock_client.chat.completions.create.return_value`: the AttributeError is raised in the test
  itself. Nothing in generate.py was to blame. The model had built the same incomplete mock in
  test_context.py in attempts 1-2.
- **Stream chunks lost their role** because `rules.MOCK_DICT_CHUNK`'s example is
  `SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=...))])`: the model
  copied it in both attempts (`delta=SimpleNamespace(content="Here's")`), and the unrelated-edit
  check rejected the missing "assistant" both times.
- **Factory target:** the suggestion was right and complete. Both attempts' check errors for
  test_context.py / test_suggestions.py name `patch("genai.generate.get_client")` with an
  example, and the retry requests carried the whole message (not cut off). The model kept
  `genai.context.get_client` anyway until attempt 3 (the "same error as last time" nudge).
  What is cut off is the report's attempt line (300 characters), which ends inside the list of
  genai/context.py's 20 top-level names, before the suggestion; that list also comes first in
  the model's message, where it is noise: no name in a module that does not use openai is a
  useful target.
- **Attempt budget:** 3 of the 4 attempts never reached verification (check_failed); the job
  got one sandbox verdict in total.

## Plan

1. **Keep the last good version.** In whole-file mode, a file accepted in an earlier attempt
   that fails its checks on a resend keeps its last accepted version (already in the working
   copy) for this attempt: it does not fail the attempt, is not set aside, and test files that
   import it are sent. Recorded per attempt as `fell_back` (`path`, `accepted_in`, `error`),
   on the file entry as `fell_back_to`, and in the report's attempt line. Its error streak is
   still counted (the nudge on a later resend), but a repeated error on a file that fell back
   does not stop the job.
2. **Mock failures go to the tests.** `verifier.mock_failure(message)`: an
   `AttributeError: '<mock>' object has no attribute 'x'` or `TypeError: '<mock>' object is
   not ...` (also `Mock object has no attribute` and `object MagicMock can't be used in 'await'
   expression`) where `<mock>` is SimpleNamespace, Mock, MagicMock, NonCallableMock,
   NonCallableMagicMock, AsyncMock or PropertyMock, wherever it is raised. When every blocking
   reason is a regression (`regression`, `regression_error`) and every newly failing or erroring
   test failed that way, the next attempt resends no non-test Python file that no reason or
   remaining finding names; test files are resent as before. In region-edited non-test files,
   traceback lines of mock-failure tests no longer add regions; the failing test's own region
   is added as before.
3. **Hint** (`Migration.mock_failure_hint`, `{problem}` filled per distinct failure, at most 3):
   "the test's mock client is incomplete: SimpleNamespace has no create; build the client mock
   with MagicMock() (or include the whole chat.completions.create chain) and set .return_value
   on create". TypeError: "SimpleNamespace is not callable". Not for the autospec form (`Mock
   object has no attribute`), which keeps its own hint. Whole-file and agent mode.
4. **Stream chunks keep the role.** A mocked chunk whose delta has a `role` key gets
   `rules.MOCK_DICT_CHUNK_ROLE`: the example has `role=<the original's value>` and the text
   says to keep the delta's keys, role too. Chunks without role keep `MOCK_DICT_CHUNK`. The
   Streaming guide part gets a mocked-chunk line with role.
5. **Factory target message:** when the patched module does not use the library and the
   suggestion comes from other modules, the message leaves out the module's top-level names
   ("genai/context.py has no top-level name 'get_client' and does not use openai. Patch openai
   where ..."), so the suggestion comes first in the model's message and in the report line.
   Replay attempt 1's replies against the check to confirm `genai.generate.get_client`.
6. **Attempt budget: attempts that never reach verification do not count** (the simpler option:
   one rule, no formula to guess; scaling with the number of files would still let a two-file
   repo spend its attempts on checks, and would need an "auto" default for `--max-retries`).
   `--max-retries N` now bounds verifications (N + 1); a whole-file attempt that ends
   `check_failed` is free, with a hard cap of 2 x (N + 1) attempts in total. The repeated-error
   stop, token and cost limits still apply. Agent mode's `no_changes` attempts still count.
   The report's Attempts line shows both.
7. **Replays** (scratch script, the run's replies, a fake sandbox giving attempt 3's
   verification): attempt 1 for the factory message; attempts 3-4 to confirm generate.py is not
   resent, the tests are sent with the hint, and a failing generate.py resend falls back.

## Built

As planned, with two additions found while building:

- A fell-back file stays pending when its attempt is not verified (otherwise nothing would
  resend it), and an attempt whose tree after fallbacks is exactly the tree the last
  verification judged is not verified again (`check_failed`, so it is free).
- `only_mock_failures` looks at `reject` reasons only: a run where every test failed also
  has `no_usable_tests` / `coverage_unavailable`, which follow from the failures and point at
  no code. A mock-failing test whose file apifix does not send keeps the old behaviour (the
  code under test is resent), since the test cannot be changed.

The budget change touched eight existing tests written for `max_retries=0` meaning one
attempt: they now script (and assert) the free second attempt.

Replays (`replay_run6.py`, offline: the run's replies per file and attempt; a region request
is answered with exactly its regions, merged from the run's group replies, missing ones as
sent; a fake sandbox giving the baseline's 38/6 and attempt 3's two regressions, with a
`genai/generate.py:124` line added to their traceback):

- **Attempt 1:** test_context.py and test_suggestions.py fail the mock-target check as in the
  run. The message now reads "mock target 'genai.context.get_client': genai/context.py has no
  top-level name 'get_client' and does not use openai. Patch openai where a module the test
  reaches uses it: genai/generate.py: generate_next_from_history() gets client =
  get_client(): patch("genai.generate.get_client") ..." with the example; the report's
  attempt line now reaches `patch("genai.generate.get_client")` before its 300-character cut.
- **Attempts 1-3** end as in the run: check_failed, check_failed, rejected (the same two
  regressions). The report counts 2 attempts that ended at the checks, of 8 in all.
- **Attempt 4, new attribution:** genai/generate.py is not resent (kept); all four test files
  are sent, each request with "Hint: the test's mock client is incomplete: SimpleNamespace has
  no create; build the client mock with MagicMock() (or include the whole
  chat.completions.create chain) and set .return_value on create". With synthetic replies that
  follow the hint (`mock.MagicMock()` for the client), the job ends verified_weak (the fake
  sandbox has no coverage).
- **Attempt 4, old attribution** (generate.py resent, the run's own two replies that put
  `"content" in delta` back): generate.py fails the streaming check and keeps its attempt-1
  version (`fell_back`), the four test files are sent instead of held back, and the attempt is
  verified.
- The scanner now gives tests/test_generate.py's chunks
  `delta=SimpleNamespace(role="assistant", content=...)` in the suggestion; whether the model
  keeps the role needs a live run.
