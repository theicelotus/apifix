# Plan: import-path heuristic must not shadow the standard library

## Request

Bug found on saulpw/aipl (run: ~/apifix-runs/aipl-gptoss-1, repo:
~/apifix-targets/aipl). Both baseline and fixed test runs crashed with
circular imports because apifix added aipl/ops/ to the import path
("added aipl/ops/ to the import path: aipl/ops/test.py imports json from
it"). aipl/ops/json.py is an aipl op, so with that directory on sys.path
it shadowed the standard library json for the whole test run. aipl's CI
just runs `pytest .`.

Fix the import-path heuristic:
1. Only files pytest would collect count as test files: the
   python_files patterns from the repo's pytest config, default
   test_*.py and *_test.py. aipl/ops/test.py is not a test file.
2. A bare import of a standard-library module name
   (sys.stdlib_module_names) never justifies adding a directory, and a
   directory is never added if it contains a module that shadows a
   standard-library name.
3. If the baseline run fails at collection after apifix changed the
   import path, retry once without the change and keep whichever
   collects tests. Report which was used.

Tests for each, with a small fixture shaped like aipl (a package with
ops/json.py and ops/test.py). Note it in docs/notes.md. Run the full
suite once, then ruff. Commit without a Co-Authored-By line, then
push to origin main.

## Plan

- `project.guess_import_paths` takes the `python_files` patterns (from `pytest_config`,
  default `test_*.py`, `*_test.py`) and only reads files whose name matches (fnmatch on the
  basename, as pytest does). The explicit test list given to pytest is unchanged.
- Names in `sys.stdlib_module_names` are skipped as import sources, and a candidate dir
  holding any module (`x.py` or `x/__init__.py`) with such a name is never added.
- `TestPlan.import_paths_guessed` marks guessed (not configured) paths. `SandboxSession.run_tests`
  re-runs the test phase without them when a run with guessed paths collected no tests; the
  retry is kept if it collects more tests. The note and `SandboxResult.import_paths` say
  which was used.

## Built

- `project.guess_import_paths`: `python_files` filter, stdlib-name and shadowing checks;
  `TestPlan.import_paths_guessed`.
- `sandbox.SandboxSession._retry_without_import_paths` and `SandboxResult.retried_without_import_paths`;
  `verification.json` field and a report line in `verifier.py`.
- Tests: `tests/test_project.py` (aipl-shaped fixture), `tests/test_sandbox.py` (retry kept /
  first run kept / no retry / configured paths). Existing fixtures named `testserver.py` became
  `test_server.py` where they exercise the import-path guess.
- Notes in `docs/notes.md` (Import paths).
