# Already migrated: stale tests on code already on openai 1.x; trailing-space directories

Requested 2026-10-23 and built in the same step (the request gave the design; no separate
review). The request is verbatim.

## Request

On Dillettant/Athena (cloned at ~/apifix-targets/athena), apifix scan --migration openai-v1
reports 8 findings, all in test files (stale
mock.patch('src.topic_gen.topic_gen_utils.openai.ChatCompletion.create') targets and a dict mock
response), while requirements.txt and Pipfile.txt pin openai==1.12.0 and
src/topic_gen/topic_gen_utils.py already uses openai.OpenAI(...) and
client.chat.completions.create. The project is already migrated; only its tests are stale.

In scan and fix, detect this case: every declared openai requirement already allows only 1.x,
there are no findings in non-test files, and (optionally) non-test code uses 1.x API names. Then
report “already on openai 1.x: the N findings are all in test files, which look out of date with
the code” and list them. fix stops before any LLM call or sandbox run, with a dedicated outcome
(e.g. already_migrated) and exit code, unless a flag such as --fix-stale-tests is given (just
record that flag in the plan’s Later section; don’t build stale-test repair now).
Separately: the walker should handle the directory src/outline_gen / (a trailing space in the
name). Check it’s scanned, and add a test.

Synthetic fixtures only. Run the fast suite and ruff (Docker only if touched), then commit.

## Findings

- Athena: `requirements.txt:37 openai==1.12.0` is the only declaration apifix reads.
  `Pipfile.txt` is not a dependency file name apifix recognises (`Pipfile` is), and
  `Pipfile.lock` is a lockfile, not a declaration. Non-test code: `openai.OpenAI(...)` at
  `src/topic_gen/topic_gen_utils.py:23`. The 8 findings are in `unittest/testGenerateTopics.py`
  (7 patch targets) and `unittest/testGenerateOutline.py` (the dict response).
- `src/outline_gen /outline_gen_utils.py` was already walked and scanned (`os.walk` keeps the
  name as is). `unified_diff` writes `a/src/outline_gen /x.py` headers, and `git apply` (as
  `apifix verify` runs it) applied such a diff in a scratch check. So only a test was missing.

## Plan (as built)

1. `engine/already_migrated.py` (stdlib only, no LLM): `detect_already_migrated(root, migration,
   findings)` returns `AlreadyMigrated` (message, declarations, new-API uses, test files) or
   None. It needs all three of these:
   - **Declarations**: at least one of the library in the repo's dependency files, and every
     one entirely at or above `Migration.first_version` (`dependencies.entirely_at_or_above`,
     the counterpart of `entirely_below`). A dependency file that does not parse means the pins
     are unknown, so the result is None.
   - **Findings**: at least one, and every one in a test file (`project.is_test_file`).
   - **New API in code**: a name only 1.x has in a non-test Python file
     (`Migration.new_api_uses`). The request made this optional. It is required here, because
     without the flag nothing can override the early stop, so the stop should only happen on
     strong evidence. Without it, `fix` runs as before.
2. openai-v1: `first_version = "1.0"`, `target_name = "openai 1.x"`, `new_api_uses` ->
   `python_checks.v1_api_uses`. These names count: `rules.V1_ONLY_NAMES` (client classes, and
   the errors 1.x added: `BadRequestError`, `APITimeoutError`, `NotFoundError`, ...) and the
   module-level client's resources `openai.chat`, `completions`, `embeddings`, `images`,
   `moderations`, `audio`, `fine_tuning`, resolved through openai imports. Names that 0.x and
   1.x share (`APIError`, `RateLimitError`) do not count, and neither does
   `client.chat.completions.create` on a client the resolver cannot trace to openai (the hosted provider and
   other SDKs copy that shape).
3. `scan`: `ScanResult.already_migrated` (in `--json` too). The text output starts with the
   message and the evidence, then lists the findings as usual. The exit code stays 1, because
   the findings are real.
4. `fix`: new outcome `already_migrated`, checked right after `no_findings`, so it comes before
   the price check, pin edits, preflight and baseline. Empty diff, 0 attempts. The report gets
   an "Already migrated" section (the evidence, then every finding with its old pattern and
   suggestion), and the trajectory gets `job.already_migrated`. The CLI prints the outcome and
   one line per finding, and exits with the new `EXIT_ALREADY_MIGRATED = 3`.
5. Tests: the synthetic fixture `tests/fixtures/stale_tests/` (requirements.txt and Pipfile on
   1.12.0, `openai.OpenAI` in `src/topics/gen.py`, tests patching
   `src.topics.gen.openai.ChatCompletion.create` with a dict response, and a `src/outline /`
   directory). `tests/test_already_migrated.py` covers detection, each negative case (0.x pin,
   unpinned, `>=0.27`, `<2`, one 0.x declaration among several, no declaration, unparsable
   dependency file, non-test finding, no 1.x names, 1.x names only in tests, no findings,
   single-file scan), `v1_api_uses`, scan text/JSON, `fix` (no LLM request, no sandbox run,
   report, trajectory) and the CLI exit code. `test_entirely_at_or_above` in
   test_dependencies.py, and `test_directory_with_trailing_space_is_scanned` in
   test_scanner.py.

## Amendments

None (built directly from the request).

## Later

- `--fix-stale-tests` (fix): when the repo is already migrated, do not stop. Run a job whose
  task is to update the out-of-date tests to the code's 1.x API (patch targets such as
  `src.x.openai.OpenAI` / the module's client, object-shaped mock responses) without touching
  non-test code. The verifier's tampering checks still apply. The baseline will usually fail
  (the tests mock an API the code no longer calls), so the verdict needs a rule for "tests
  that failed before and pass after, with only test files changed".
