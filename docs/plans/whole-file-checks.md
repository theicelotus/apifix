# Plan: whole-file pre-apply checks (undefined names, mock targets) and timing

Requested 2026-10-14, to be built directly (no separate review). The request is verbatim.

## Request

Context: This is apifix. Read CLAUDE.md and docs/plans/whole-file-mode.md first. Whole-file mode
was committed in d97d8ed. A live run of test_local_whole_file_fix_completes against
qwen2.5-coder-7b was rejected in all 3 attempts. Its trajectory is at
~/apifix-runs/wholefile-7b.json; read it to get the model's exact app.py and tests/test_app.py
from the last attempt.

What went wrong: app.py and requirements.txt were migrated correctly, but tests/test_app.py (1)
used SimpleNamespace without importing it, and (2) patched app.client.chat.completions.create
while app.py created client inside ask(), so app has no attribute client. The verifier's failure
output reached the model ("AttributeError: module 'app' has no attribute 'client'") and it
repeated the mistake. Both bugs can be found statically and reported precisely in the file's own
conversation, through the existing in-file retry (check_retry).

Task: two new pre-apply checks for whole-file mode, plus timing in the trajectory. Record this
request verbatim in a new plan file, docs/plans/whole-file-checks.md, then build it. If anything
below conflicts with the code, stop and ask instead of guessing.

1. Undefined-name check (Python files).

Use pyflakes (add it as a dependency) and report only its undefined-name messages.
Only names that are undefined in the candidate but not in the original count, so existing code
that relies on star imports or builtins can't fail.
Error message format: line N: 'SimpleNamespace' is used but never imported or defined. For common
standard-library names (SimpleNamespace, MagicMock, patch, Mock, dataclass, Path and similar), add
the import line to the message, e.g. add: from types import SimpleNamespace.

2. Mock-target check (test files).

Find string targets in calls and decorators whose function is patch, mock.patch,
unittest.mock.patch or mocker.patch (not patch.object or patch.dict), first argument a string
literal.
Only check targets that are new or changed compared to the original file.
If the leading parts of the target name a module in the repo (e.g. app → app.py, pkg.mod →
pkg/mod.py or pkg/mod/__init__.py), the next part must be a top-level name in that module's
current working-copy contents (after files already accepted in this attempt): a def, class,
assignment or import. Targets outside the repo (e.g. openai.resources...) are not checked.
Error message format: mock target 'app.client.chat.completions.create': app.py has no top-level
name 'client' (its top-level names: ask, openai). Patch a name that exists in app.py.

Both checks go into check_candidate alongside the existing ones, are recorded in checks per
reply, and use the existing in-file retry. Put the analysis code in its own module so agent mode
can use it later; whole-file mode only for now.

3. Timing. Record duration_s for each LLM response and each sandbox run in the trajectory if it
isn't there already, and add a one-line time breakdown to the report (LLM time vs sandbox time).

Tests: both bugs from the live run as regression cases (the exact test_app.py and app.py from the
trajectory), names already undefined in the original, star imports, module-level vs
function-level definitions, pkg/mod and __init__.py resolution, targets outside the repo,
unchanged targets, patch.object ignored, and the retry carrying each message. Run the full suite
with --docker and ruff, then commit. Don't run the live tests.

## Plan (as built)

### New module `engine/code_checks.py`

Pure functions (source text in, messages out; module lookup through a callback), no agent or
whole-file imports, so agent mode can call them later.

- `undefined_names(path, original, candidate) -> list[str]`
  - pyflakes `Checker` on both versions; only `messages.UndefinedName` counts (not
    `UndefinedLocal`, `UndefinedExport`, or star-import "may be undefined" messages).
  - A name counts if it is undefined in the candidate and not undefined anywhere in the original.
  - One message per name, at its first line: `line N: 'X' is used but never imported or
    defined`, plus ` (add: from types import SimpleNamespace)` for names in a small table of
    common standard-library names (`types`, `unittest.mock`, `dataclasses`, `pathlib`, `typing`,
    `collections`, `functools` and a few module names such as `json`, `os`, `re`).
  - If either version does not parse, nothing is reported (the syntax check covers that).
- `mock_targets(source) -> list[(line, target)]`: string first arguments of calls (decorators
  included) to `patch`, `mock.patch`, `unittest.mock.patch`, `mocker.patch`. `patch.object`,
  `patch.dict` and aliases are not matched.
- `mock_target_problems(original, candidate, read_module) -> list[str]`
  - Only targets that appear in the candidate and not in the original.
  - `read_module(relpath) -> str | None` returns the working copy's text of a repo file.
  - Resolution: for prefixes of the target from longest (all parts but the last) to shortest,
    try `<prefix>.py` and `<prefix>/__init__.py`, at the repo root and under `src/`. The first
    module found decides; no module found means the target is outside the repo (not checked).
  - The next part passes if it is a top-level name of that module, or itself a module or package
    in the repo (`patch("pkg.mod")` style targets).
  - Top-level names: `def`/`async def`/`class`, assignment targets (`=`, annotated, augmented,
    tuple unpacking), imports (`import a.b` binds `a`), including inside module-level
    `if`/`try`/`with`/`for`/`while` blocks; plus names declared `global` in a function.
  - Not checked (no false failures): the module does not parse, has a star import, or defines a
    module-level `__getattr__`.
  - Message: `mock target 'app.client.chat.completions.create': app.py has no top-level name
    'client' (its top-level names: ask, openai). Patch a name that exists in app.py` (names
    sorted, at most 20 listed).

Messages carry no trailing period: `check_candidate` joins errors with `; ` and
`check_retry_message` appends `. Return the complete corrected file ...`.

### `whole_file.check_candidate`

- New keyword `read_module: Callable[[str], str | None] | None = None`.
- New check names, recorded per reply in `checks`:
  - `undefined_names` (Python files whose syntax check passed),
  - `mock_targets` (test files per `tampering.is_test_file`, syntax ok, and `read_module` given).
- Errors join the existing list, so the existing one-per-file `check_retry` carries them.

### `agent.py`

- `_whole_file_request` passes a reader over the working copy (`self.work`) that returns the
  candidate itself for the file being checked. Accepted files are written immediately, so this is
  the working copy "after files already accepted in this attempt".
- Timing:
  - each `attempt.responses[]` entry gets `duration_s` (wall clock of `llm.complete`);
  - `run_summary` (verifier) gets `duration_s`, so the trajectory's `baseline` and every
    `verification.runs.{baseline,fixed,rerun}` carry it (the sandbox's own figure: install plus
    test phase; a session's runs share one install, so per-run figures overlap);
  - each attempt gets `verification_duration_s` (wall clock of `verify()`), the job a
    `baseline_duration_s`; the trajectory gets `timing: {llm_s, sandbox_s}`;
  - the report gets one line, e.g. `- Time: 7,921 s total; LLM 7,512 s (9 responses); sandbox
    380 s (baseline and 3 verifications)`. Sandbox time is the wall clock of the baseline run and
    `verify()` calls, so the shared install is counted once.

### Tests

- `tests/test_code_checks.py`: the live run's `tests/test_app.py` and `app.py` (verbatim from the
  trajectory) for both bugs; names already undefined in the original; star imports; builtins;
  module- vs function-level definitions; `pkg/mod.py` and `pkg/mod/__init__.py` (and `src/`)
  resolution; submodule targets; targets outside the repo; unchanged targets; `patch.object` and
  `patch.dict` ignored; `mocker.patch` and `unittest.mock.patch` matched; unparsable / star
  import / `__getattr__` modules skipped.
- `tests/test_whole_file.py`: `check_candidate` records both checks (and `n/a` cases).
- `tests/test_agent.py`: the live run's replay in whole-file mode: the test file's first reply
  is rejected with both messages through `check_retry`, a fixed reply is accepted; response and
  sandbox timing in the trajectory and the report line.

### Docs

CLAUDE.md: layout entry for `code_checks.py`, the pre-apply checks in the whole-file limitations,
and pyflakes as the one runtime dependency.
