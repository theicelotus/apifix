# Dummy keys in the sandbox, module-level guards, missing repo modules

## Request (verbatim)

Follow-ups from ailabel-genai-2 (verified) vs -genai-4 (verified_weak).
ailabel/lib/llms.py:27-32 raises ValueError("API key not found...") at
import time when GOOGLE_API_KEY/GEMINI_API_KEY are unset. The sandbox sets
neither, so tests importing llms.py error on the original AND the fix.

1. Dummy keys in the test phase: set the migration's key variables
   (google-genai: GOOGLE_API_KEY, GEMINI_API_KEY; openai-v1: OPENAI_API_KEY;
   take them from the migration's key markers) to an obviously fake value in
   both runs, unless the repo's own test config sets them. Network is off,
   so this is safe. Say so in the report ("sandbox set dummy GOOGLE_API_KEY,
   GEMINI_API_KEY").
2. Check ailabel-genai-2's fix.diff: if it removed or moved the module-level
   key check in llms.py, that is an unrelated behavior change. The
   unrelated-edit check must catch removing a module-level raise/guard, the
   way it catches deleted retry guards.
3. Weak retry: a test that fails importing a repo module that does not
   exist in the checkout (tests/entrypoints/test_cli.py ->
   ailabel.label_prediction) can't be fixed by the migration: record "not
   retried: missing repo module ailabel.label_prediction" instead of
   retrying.

Tests for each. Write the plan doc. Run the full suite once (whole suite),
then ruff. Commit without Co-Authored-By lines and push to origin main.

## Findings

- genai-2's fix.diff removes the module-level `if not api_key: raise ValueError(...)` block and
  `genai.configure(...)` from `ailabel/lib/llms.py`: the check was dropped, not moved.
- The migrations already carry the variable names (`ci_env_placeholders`, the same names as
  `client_key.envs` / the key markers) with a fake value each; they were only passed to the
  sandbox when a CI workflow set the variable.
- genai-4's traceback runs through `ailabel/entrypoints/cli.py` (not edited by the fix) to
  `from ailabel.label_prediction import ...`; `ailabel/` exists, `label_prediction` does not.

## Plan

1. `project.sandbox_env_vars(repo, names)`: every migration key variable gets its placeholder
   (name -> origin: the workflow file when CI sets it, else `default`), except those the repo's
   own test setup sets (`pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `.env`,
   `.env.test`, a `conftest.py` that writes the environment). Used by the agent and
   `apifix verify`, so baseline and fixed runs get the same env. `sandbox_env_lines` writes the
   report bullets: the CI wording for CI-set variables, "sandbox set dummy A, B" for the rest.
2. `unrelated._dropped_module_guards` (part of `_drift`): module-level `if ...: raise` blocks of
   the original that the candidate has fewer of at module level (removed, or moved into a
   function) are a check error sent back to the model. Matched by count so a renamed variable in
   the condition is fine.
3. `weak_retry.missing_repo_module`: a collection `No module named 'pkg.sub'` where `pkg` is a
   directory in the checkout and `pkg/sub(.py)` is not, and the traceback does not run through a
   file the fix edited, is not retried: `split_environmental` returns a third dict and the
   agent records `not retried: missing repo module pkg.sub (...)`.
4. Tests for each; CLAUDE.md hard rule reworded; full suite, ruff, commit, push.

## Consequences

- The "uncalled mock with a keyless client" hint assumes the tests run without a key. With the
  dummy key set that premise is false, so the hint no longer fires by default (its test now
  asserts that).
- Reports and trajectories (`sandbox_env_placeholders`) now list the dummy variables too, as
  name -> `default` or the workflow file.

## Built

- `project.py`: `repo_test_env_vars`, `sandbox_env_vars`, `sandbox_env_lines`, `DEFAULT_ENV_ORIGIN`;
  `agent.py` and `verify_diff.py` use them (report bullet: "sandbox set dummy A, B").
- `unrelated.py`: `_dropped_module_guards`, called from `_drift`.
- `weak_retry.py`: `missing_repo_module`; `split_environmental` returns a third dict; the agent
  records `not retried: missing repo module <name>`.
- CLAUDE.md hard rule reworded for the dummy values.
- Tests: `test_project.py` (env vars, config sources, report lines), `test_agent.py` (dummy key
  in both runs and the report, none when the repo's pytest.ini sets it, hint no longer fires),
  `test_unrelated.py` (kept / removed / moved / in-function guards), `test_weak_retry.py` (split
  and the agent's recorded skip). Full suite: 2226 passed (Docker).
