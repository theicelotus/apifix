# llmflows run 3: engine= in call assertions, and a cut-off reply under a per-minute limit

## Request

Run ~/apifix-runs/llmflows-gptoss-3 stopped on a hosted endpoint's daily token limit after
138,732 tokens. Diagnose from that run folder (report.md, verification.json,
trajectory.json) before changing code:

1. Attempts 3 and 4 failed tests/llms/test_openai_embeddings.py with "changed
   code unrelated to the migration ("engine="test_model",")" at original lines
   35 and 61. Look at those lines in ~/apifix-targets/llmflows. If engine= there
   is part of an assertion on the openai call (assert_called_once_with,
   call_args, etc.) or is passed into code that now sends model=, then changing
   it is part of the migration. In that case the unrelated-edits check must
   accept engine=/deployment_id= -> model= in test files. Check
   verification.json: were attempts 5 and 6 rejected because that test still
   expected engine=? Tell me in plain words what rejected each one.

2. Attempt 6 cut off test_openai_embeddings.py at 3,053 reply tokens even with
   --max-output-tokens 6000, then said "no smaller request left". Explain why,
   and whether a region or batch retry should have run instead.

Fix the parts that are apifix bugs and add tests for them. Write
docs/plans/llmflows-run-3.md with this request verbatim plus a "Built"
section, then commit. Show what the checks now say about the attempt 3/4
version of test_openai_embeddings.py from trajectory.json, without calling
the LLM.

## What the run shows

verification.json is `null`: the job aborted (daily limit) before a final verdict. The
per-attempt verdicts are in trajectory.json (`attempts[].verification`); only attempts 5
and 6 reached the sandbox.

1. Original lines 35 and 61 are `engine="test_model",` inside
   `mock_openai_embedding_create.assert_called_once_with(...)`; the mock is
   `@patch("openai.Embedding.create", autospec=True)`, and `OpenAIEmbeddings.generate` passes
   `engine=self.model` to that method through `call_with_retry(func=..., ...)`. Migrating the
   call to `model=` makes the assertion's `engine=` wrong, so changing it is the migration.
   The unrelated-edit check judged the one-line hunk `engine=` -> `model=` as code away from
   every finding (the nearest, the `@patch` line, is 14 lines up) with no openai-bound name in
   it (the mock's name is on the line above the hunk), so it rejected it. Attempts 3 and 4
   sent the same file and got the same error.
   - Attempt 5 (rejected): the model kept `engine=` in the test (as the check demanded), the
     source sent `model=`, so both embeddings tests failed with "expected call not found.
     Expected: create(engine='test_model', ...) Actual: create(model='test_model', ...)":
     a regression caused by the stale assertion. Yes, rejected because the test still
     expected `engine=`.
   - Attempt 6 (rejected): to satisfy that same assertion, the model rewrote
     `llmflows/llms/openai_embeddings.py` back to `openai.Embedding.create(engine=...)` and
     `.acreate`. Every whole-file check passed it; in the sandbox both embeddings tests
     raised `APIRemovedInV1` ("You tried to access openai.Embedding"), and the verifier also
     reported four `incomplete_migration` findings in that file. The test file's own request
     was cut off (part 2), so it kept its attempt 5 version. Indirectly also the stale
     `engine=` assertion.
   - Replaying attempt 3's diff with the attempt 3/4 test file through `apifix verify`
     (sandbox, no LLM): `verified_weak`, weak only on changed-line coverage (8 of 20 lines).
     Without the false positive the run would most likely have ended at attempt 3.
2. the hosted provider's per-minute limit (8,000 tokens) was known from an earlier refusal. The request's
   prompt was 4,661 tokens, so apifix capped max_tokens from the file's budget (3,738) to
   3,053 (8,000 - 4,661 - 256 margin). `--max-output-tokens 6000` cannot raise that: the
   server counts prompt plus max_tokens against the limit. The reply spent 2,351 of the
   3,053 tokens reasoning (this file's earlier replies: 858 to 1,494), leaving about 700 for
   a file of about 700. After the cut-off: "more reply tokens" was not possible (the capped
   most equals what was sent), and regions were not tried because the two test methods plus
   imports are 85% of the file (regions count as smaller only under 80%); batches split only
   region requests. So "no smaller request left" was correct by apifix's rules, and a
   region or batch retry would not have helped much: it would still carry the guide, rules
   and feedback, so the reply cap would stay about the same, and reasoning, not file text,
   used the budget. What could have worked is more room (a leaner prompt without the context
   files and previous version, about 1,100 tokens) or less reasoning (`--reasoning-effort
   low`, which the report already suggests). The real fault was the message: it gave no
   reason for 3,053, so it looked like `--max-output-tokens` had been ignored.

## Plan

- Migration data: `Migration.keyword_renames` (openai-v1: `rules.KWARG_RENAMES`,
  `engine`/`deployment_id` -> `model`, `request_timeout` -> `timeout`: the KWARG_CHANGES
  that are plain renames).
- unrelated.py: with `renames` (given for test files only), a hunk whose only change is one
  of them, as a keyword argument or a plain string equal to the old name
  (`call_args.kwargs["engine"]`, which was a text-only hunk and was silently reverted), is
  kept wherever it is; its renamed strings are not "missing". A hunk with any other change is
  judged as before.
- whole_file.check_candidate takes `keyword_renames` and passes them for test files; the job
  passes the migration's.
- Cut-off message: add the reasoning share and, when a known per-minute limit capped
  max_tokens below `--max-output-tokens`, say so.
- Not built (written to docs/notes.md as known gaps): re-scanning a rewrite for 0.x calls it
  left in or put back (attempt 6); a leaner-prompt resend after a cut-off under a
  per-minute limit.

## Built

- `rules.KWARG_RENAMES` and `OpenAIV1Migration.keyword_renames`; `Migration.keyword_renames`
  in the protocol.
- `unrelated.py`: hunk kind `rename` (`_Diff._rename_only`, string-exact token keys
  `_exact_keys`), kept; `_missing_strings` exempts renamed strings on rename hunks' lines;
  `revert_text_edits` / `unrelated_edits` take `renames`.
- `whole_file.check_candidate(keyword_renames=...)`, applied when the file is a test file;
  `_Job._check` passes `self.migration.keyword_renames`.
- `_Job._cutoff_detail` and `_truncated_error(..., detail)`: a failed cut-off now reads, for
  attempt 6, "(at 3,053 reply tokens, 2,351 of them reasoning; capped so the 4,661-token
  prompt fits the server's 8,000 tokens-per-minute limit: --max-output-tokens 6,000 cannot
  raise it)". Whole-file and region requests.
- Fixture `tests/fixtures/kwarg_assertions` (the llmflows shape: `call_with_retry(func=
  openai.Embedding.create, engine=..., ...)`, tests asserting `engine=`,
  `call_args.kwargs["engine"]` and `deployment_id=` 6 to 7 lines from a finding).
- Tests: `test_unrelated.py` (engine= and deployment_id= assertions accepted, rejected without
  the renames as in the run; the kwargs key kept instead of reverted; a rename combined with
  another change still rejected and a key changed to another name still reverted; renames
  not applied to non-test files; renames match KWARG_CHANGES), `test_agent.py` (the job
  passes the renames; the cut-off message).
- docs/notes.md: the rename rule under unrelated edits; the cut-off detail and the
  per-minute-room gap; the attempt 6 rollback gap.
- Checks on the attempt 3/4 file from trajectory.json (no LLM), now: syntax, abbreviation,
  undefined_names, mock_targets, call_assertions, unrelated_edits, config, streaming all ok;
  the two comment edits (lines 33 and 59, far from findings) reverted as before; accepted.
  Before: unrelated_edits failed at original lines 35 and 61, as in the run.
