# Behavior-drift checks, --sandbox-extra, baseline environment rot

## Request (verbatim)

Follow-ups from agentic-tictactoe-genai-6 (+ -verify2..4).

1. Behavior drift inside migrated lines: the fix changed
   time.sleep(wait_time) -> time.sleep(2**attempt) (drops the server's
   retry-after) and `) from e` -> `)` (drops exception chaining). Add checks:
   a `raise ... from X` must keep its `from X`; an argument variable passed
   to a call in the original (wait_time) must not be replaced by a different
   expression unless the migration renames it. Report them as unrelated edits.
2. --sandbox-requirements replaces detection entirely, which made it easy to
   break the baseline (I dropped google-generativeai and the original code
   ran without its SDK). Add --sandbox-extra FILE that adds to the detected
   requirements instead, and warn when --sandbox-requirements leaves out a
   distribution the migration's old side needs.
3. Baseline environment rot: when the baseline's failures include
   ImportError/AttributeError at import of a package that contains changed
   files (e.g. "module 'src' has no attribute 'llm'" from patch()), say so in
   "Why this verdict": the changed code could not be imported by the tests
   in either run, name the underlying import error, and suggest pinning it.

Tests for each. Write the plan doc. Run the full suite once, then ruff.
Commit without Co-Authored-By lines and push to origin main.

## Plan

### 1. Drift checks (`unrelated.py`)

Hunks near a finding are always kept, so drift inside a migrated statement passes the hunk
judgement. A second pass, `_drift`, compares the original and candidate ASTs; its errors join
`unrelated_edits()` (same retry path, same "original line N" numbering):

- **raise-from**: per scope, every `raise ... from Y` of the original needs a candidate raise
  with a cause: the same text first, else any spare one (a renamed `as e` -> `as err`). A cause
  that is gone is an error.
- **argument variables**: calls pair up by scope and callee text (identical calls first, the
  rest by order when their numbers match). A plain variable argument of the original that
  became another expression is an error, unless the new expression is still built from the
  variable (`response` -> `response.model_dump()`), or it is a plain name and the old one is
  used nowhere in the candidate (renamed throughout). A keyword the migration renames is looked
  up under its new name. Calls on a finding's own lines are skipped.

### 2. `--sandbox-extra FILE`

`SandboxRequirements` gets `extra: bool`; the label is `--sandbox-extra <name>`. In
`build_install_plan`, an extra file is applied on top of normal detection: its lines replace
detected requirements of the same name (and the fix's own dependency edits for a name win, as
with the override), the rest are added. Mutually exclusive with `--sandbox-requirements` in the
CLI. Reports say "adding to apifix's detection".

Warning: `warn_missing_old_side()` in `project.py` lists the migration's old-side distribution
(`source_package`, else `package`) when a `--sandbox-requirements` file does not name it; the CLI
prints it to stderr and suggests `--sandbox-extra`.

### 3. Baseline environment rot (`verifier.py`)

New reason `baseline_import_rot` (severity `weak`, so it shows in "Why this verdict"):
a test that fails or errors in both runs with `module 'P' has no attribute 'A'` or an
`ImportError`/`ModuleNotFoundError` naming P, where P is a module or package that contains a
changed Python file. The message says the changed code could not be imported by the tests in
either run, names the underlying import error found in the baseline's test output (first
`ImportError`/`ModuleNotFoundError` line not about P itself, else the first one), and suggests
pinning the dependency (`--sandbox-extra`).

## Approval amendments

None; the request said to build and push.

## Built

- `unrelated.py`: `_drift` (`_dropped_causes`, `_replaced_arguments`) called from
  `unrelated_edits`. `tests/test_drift.py`.
- `project.py`: `SandboxRequirements.extra`, `load_sandbox_requirements(extra=)`, `_extra_plan`,
  `missing_old_side`. `cli.py`: `--sandbox-extra` (mutually exclusive with
  `--sandbox-requirements`), stderr warning from `_sandbox`. Reports say "added to apifix's
  detection". Tests in `tests/test_project.py`, `tests/test_cli.py`.
- `verifier.py`: `baseline_rot_reasons` (`baseline_import_rot`, weak). Tests in
  `tests/test_verifier.py`.

Known gaps: the argument check ignores `*args`/`**kwargs` and calls whose callee text changed;
the rot check needs the failure text to name the module (the clipped message of a test, not
its full traceback); the underlying error is the first `ImportError` line in the original's
output that is not about a changed module.
