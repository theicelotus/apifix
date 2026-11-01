# Plan: retry before settling for weak evidence

Requested 2026-10-31, to be built directly. The request is verbatim.

## Request

Retry before settling for weak evidence (ailabel-genai-2 vs -genai-3, same
repo, same model): genai-2 got tests/lib/test_llms.py's old-broken tests
passing (fixed 14/5/1 -> verified); genai-3 edited the same file but left
them failing (10/5/2 both sides) and apifix accepted verified_weak on
attempt 1 with 3 attempts left.

When a verification would end verified_weak and some tests that fail on
the original code are in test files the fix edited (or test files that
patch rewritten code) and still fail after the fix, spend another attempt
(if any remain): send those failures back, like a regression retry, asking
to make the migrated tests pass without weakening them. Keep the best
result: never return something worse than the first verified_weak. Report
the extra attempt and why it was made.

Tests for it. Write the plan doc. Run the full suite once — the whole
suite, not just the touched files — then ruff. Commit without
Co-Authored-By lines and push to origin main.

## Plan

- New `engine/weak_retry.py`. `still_failing_migrated(v, work, api_modules)`: the tests in
  `delta.no_signal_preexisting` (fail before and after; the network-needing
  `no_signal_external` ones are left out) whose test file the fix edited, or that patches
  (`patch`, `monkeypatch.setattr`, ... with a string target, by `ast`) a module the fix
  rewrote or the migrated library. Also the feedback text (`retry_prompt`: the tests, why each
  counts, their output on the fixed code, "make them pass without weakening, skipping or
  deleting") and the report sentence (`retry_reason`).
- `agent.py`, after a `verified_weak` verification: record those tests on the attempt
  (`weak_tests`). If there are any, and an attempt is left (`max_retries`, whole-file's total),
  the environment is not what is wrong (the reasons that already stop regression retries),
  and the weak result is the first or leaves strictly fewer such tests than every earlier
  weak one, make another attempt (`weak_retry` holds why). Agent mode sends the prompt as the
  next user message; whole-file mode resends the failing tests' files (or, when those are not
  sent, the code under test they reach) with the prompt as the feedback.
- Keep the best: `best_attempt()` ranks weak attempts by fewer still-failing migrated tests,
  the earlier on a tie, so the final tree and verdict are never worse than the first weak one.
  A later attempt that is rejected, unverified, has no changes, or hits a limit (cost, tokens)
  ends the job with the best weak (or verified) attempt restored, not "rejected"/"aborted".
  If the extra attempt reaches `verified`, that is the result.
- Report: trajectory attempts get `weak_tests` and `weak_retry`; the report has an "Extra
  attempt N, before settling for weak evidence: ..." line with how it ended.
- `verifier._module_names` becomes public (`module_names`) for the patch-target match.

## Approval amendments

None.

## Built

As planned. `tests/test_weak_retry.py`: the extra attempt fixes the tests (agent mode), a
worse extra attempt keeps the first weak result, an extra attempt that leaves as many tests
failing is not better, no extra attempt without attempts left or for a failing test the fix did
not touch, whole-file mode resends the test file, candidate detection (edited / patching / other),
prompt truncation.
