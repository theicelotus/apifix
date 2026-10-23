# Why no-signal tests fail

## Request

In apifix verify on SuspensePerception (~/apifix-targets/suspense, report at ~/apifix-runs/suspense-verify-3/), 11 of 13 tests fail before and after the fix and are listed under “Tests that give no signal”, but verification.json and trajectory.json keep only their names, not why they fail.

Keep the failure messages for tests in the no-signal lists (both runs, clipped as for other failures), and show the first line of the original run’s message next to each name in the report (e.g. “tests.test_api::test_generate_response_openai: already failing before the fix: KeyError: ‘api_key’”).
Diagnose: run the original code’s tests for SuspensePerception in the sandbox the way verify does (same --sandbox-requirements ~/apifix-runs/suspense-sandbox-reqs.txt) and tell me why tests/test_api.py::test_generate_response_openai and tests/test_misc.py::test_run_experiment fail. Is it something the sandbox could reasonably provide (an environment variable, a config file path, the working directory), or a bug in the repo’s own tests? Don’t change apifix’s behaviour for this repo specifically.

Tests for item 1. Run the fast suite, Docker if the network allows, and ruff, then commit.

## Plan (as built)

- `TestDelta.no_signal_messages`: each `no_signal_preexisting` / `no_signal_external` entry
  (tests and modules failing to collect) -> `{"baseline": ..., "fixed": ...}`, the runs'
  `TestCaseResult.message`, which the parsers already clip to `MAX_MESSAGE` (500) for every
  test. It is part of `asdict(delta)`, so verification.json (`tests.no_signal_messages`) and
  the trajectories (`verification.tests`, also per attempt in `fix`) carry it.
- Report, "Tests that give no signal": `` `id`: already failing before the fix: `<first
  line>` `` from the original run's message, as inline code (test output is untrusted), at
  most 160 characters. pytest's JUnit message for a collection error is the bare line
  "collection failure"; the line after it ("ImportError while importing test module ...")
  is shown instead. No message: the bullet stays as before.

## Diagnosis (original code, sandbox as verify runs it)

Run with the same `Sandbox(SandboxConfig(pip_cache_dir=..., requirements_override=...))` and
env placeholders as `verify_diff` (none: the repo's workflows set no `OPENAI_API_KEY`).
pytest 9.1.1, Python 3.11; 9 failed, 2 errors, 2 passed.

- `tests/test_api.py::test_generate_response_openai`: error at setup, `Failed: Fixture
  "response" called directly.` conftest.py's session fixture `mock_openai` calls the
  `response` fixture as a function (`response()`), which pytest has refused since 4.0 (the
  repo's own uv.lock pins pytest 8.3.5 / 8.4.1, which refuse it too). Past that, the test's
  `model_config` has no `repetition_penalty`, which `generate_response` reads with `[...]`,
  so it would fail with `KeyError: 'repetition_penalty'`. Repo test bug.
- `tests/test_misc.py::test_run_experiment`: `TypeError: run_experiment() missing 1 required
  positional argument: 'version_prompts'`. `run_experiment` takes five arguments
  (`parse_model_config` was added; `src/thriller/Thriller.py` passes it), the test passes
  four. Repo test bug.

Neither is something the sandbox could provide (no env variable, config path or working
directory is involved), so apifix's behaviour does not change.
