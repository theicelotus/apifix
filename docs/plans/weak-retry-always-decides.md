# Weak-evidence retry: always decide, and say why

## Request (verbatim)

Weak-evidence retry didn't fire on a real repo (~/apifix-runs/ailabel-genai-4).
trajectory: attempts[1].weak_tests = {"tests.entrypoints.test_cli": "in
tests/entrypoints/test_cli.py, which patches code the fix rewrote"} but
weak_retry = "" and the job ended verified_weak with attempts left. That
module fails to import on both sides (ImportError while importing test
module), and in ailabel-genai-2 the fix made it import and its 4 tests pass.

1. Find why it didn't retry (probably the environment guard treating a
   collection ImportError as an environment problem). An import error whose
   traceback runs through a file the fix changed, a module that patches
   rewritten code, or the migrated library is migration-related: retry.
   Keep skipping truly environmental ones (missing third-party package
   unrelated to the migration).
2. Always record the decision: when weak_tests is non-empty and no retry
   happens, weak_retry must say why, and the report must show it.
3. Read the actual ImportError for test_cli in ailabel-genai-4's
   verification.json and build a fixture shaped like it.

Tests for each. Write the plan doc. Run the full suite once (whole suite),
then ruff. Commit without Co-Authored-By lines and push to origin main.

## Findings

- The environment guard was not the cause: the run's verification has no `environment_incomplete`
  reason. The run was whole-file mode; `_after_weak` found nothing to resend (`tests/entrypoints/
  test_cli.py` is not a file the model is sent, and it imports nothing sendable the traceback
  reaches), returned empty, and `_extra_attempt` returned False without recording anything.
- The stored ImportError is `ImportError while importing test module` through `tests/.../test_cli.py:6`
  and `ailabel/entrypoints/cli.py:16` (`from ailabel.label_prediction import ...`, a module the
  checkout does not have), cut off by the stored message limit.

## Plan

- `_after_weak`: when nothing the failing tests reach is sendable, resend the sendable files the
  fix edited (they are the code the test patches/imports).
- `weak_retry.py`: `split_environmental` drops weak tests whose collection output is an import
  error for a missing third-party package (not a repo module, not the migrated library, traceback
  not through an edited file, module named in the message). Unknown or cut-off messages count as
  migration-related. `retry_prompt` shows collection-failure output too.
- Every decision is recorded: `weak_retry` is the retry reason or `not retried: <why>`; new
  `weak_retried` flag; the report prints a "No extra attempt before settling for weak evidence"
  line.
- Tests: fixture shaped like the ailabel ImportError, split cases, prompt, skip text, whole-file
  fallback, recorded skip in the report.

## Built

As planned. See tests/test_weak_retry.py.
