# Plan: per-test time limit in the sandbox

## Request

Regression: `apifix verify` on benchmark/live/2026-10-31-kaizen-agent-gptoss/fix.diff
now gives unverified/no_baseline ("the original code's test run failed (tests
exceeded 600s)"), twice, alone on the machine (~/apifix-runs/regress-kaizen-agent,
-2). Last night the same verify (~/apifix-runs/kaizen-agent-genai-4-verify, apifix
3f67723, dummy keys already on) had a baseline of 4 passed / 1 failed / 1 error.

1. Find the cause: which test hangs, and why. Compare the two runs' installed
   dependency versions (whatever verification.json records) and apifix
   versions (3f67723 vs HEAD). If a package changed, name it and its version.
   If an apifix commit changed it, bisect and name the commit. If the dummy
   key makes the old SDK attempt a real call that retries for minutes with
   the network off, say so.
2. Robustness, whatever the cause: one hanging test must not cost the whole
   baseline. Give each test a time limit in the sandbox (pytest-timeout, or
   the runner's own), report timed-out tests by name in both runs, and
   treat a test that times out on both sides like any test that fails on
   both (no signal), not as no_baseline.
3. If the cause is the old SDK's network retries under a dummy key, make
   such calls fail fast in the sandbox (e.g. a short client timeout via env
   if the SDK supports it), and say what you chose.

Tests for each. Write the plan doc. Run the full suite once (whole suite),
then ruff. Commit without Co-Authored-By lines and push to origin main.

## Findings (1)

- Hanging test: `kaizen.cli.commands.test_memory_system::test_memory_basic_functionality`,
  original code only. It calls `google-generativeai` 0.8.6 (gRPC) with the dummy key; the
  call retries against the dead network until stopped.
- Last night's baseline took 652 s (install ~47 s), so its test phase ended ~10 s under the
  600 s cap. It was already marginal; run-to-run variance pushed it over.
- No package change explains it. The runs record no installed versions, so the pip cache was
  compared: the only new download is `openai` 3.42.0 -> 3.42.1 (an unpinned kaizen
  dependency), which the test does not use. No apifix change between 3f67723 and HEAD touches sandbox.py, sandbox_driver.py or project.py (git diff --stat), i.e.
  the test phase; the 6 commits are fixing and scoring code, so no bisect was needed.

## Plan

2. A pytest plugin (`engine/sandbox_plugin/apifix_timeout.py`, no install needed, so it works
   offline) loaded by the driver: SIGALRM limit on each of setup/call/teardown
   (`SandboxConfig.per_test_timeout_s`, 120 s), plus a session budget
   (`test_timeout_s` - 30 s) after which the remaining tests fail at once, so the JUnit report
   is always written. Failures carry a marker; `SandboxResult.timed_out_tests` lists them and
   is in verification.json (`run_summary`) and the report, per run. A timed-out test is an
   ordinary failed test, so failing on both sides is no signal (no_signal_preexisting).
   Skipped if the repo uses pytest-timeout itself.
3. Chosen: no SDK-specific knob. The old Gemini SDK has no environment variable for its
   client timeout or retry deadline (only per-call `request_options`), so apifix does not
   patch it; the generic per-test limit is the fail-fast. Revisit if a migration's SDK
   offers an env switch.

## Built

- Plugin, driver flags (`--per-test-timeout`, `--budget`), sandbox wiring, report and
  verification.json fields; tests in tests/test_test_timeout.py.
- Live check: the same verify now finishes in 3m45s (baseline 172 s), 4/1/1 as last night,
  the hanging test named, verdict verified_weak as before.
