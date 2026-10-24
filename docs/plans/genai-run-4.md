# genai run 4: test dependency groups, pytest usage errors, openai releases that work with httpx 0.28

## Request

Context: Read CLAUDE.md. genai run ~/apifix-runs/genai-gptoss-4/ (~/apifix-targets/genai): all 5 files passed their checks in attempt 1, but verification couldn’t run, and attempt 2 then hit the hosted provider’s daily token limit. Record this request verbatim in docs/plans/genai-run-4.md, investigate, then build.

pytest exit 4 on both versions (“pytest exited 4 without a report”): find the cause from the trajectory and the repo’s pytest configuration. I suspect addopts uses a plugin option (e.g. --cov) whose plugin is only declared in a Poetry dev/test group that apifix doesn’t install. If so, install the repo’s test-related dependency groups (Poetry group.dev/group.test, dev-dependencies, PEP 735 dependency-groups, [project.optional-dependencies] named test/tests/dev) for both runs, recorded in the report. Also, when pytest exits 4, show its usage error in the report and mark it as an environment problem rather than sandbox_unavailable.
The proxies smoke-check failure: openai 1.39 was installed with httpx 0.28.1. Find where typing-extensions==4.7.1 and tqdm==4.65.0 come from, and why the companion-pin logic didn’t raise them. Fix it so a migrated project resolves to an openai release that works with current httpx. Consider whether the target pin’s lower bound should also exclude openai releases known to break with httpx 0.28 (the proxies removal; fixed in openai 1.55.3), and explain the choice.
Re-check with apifix verify on attempt 1’s diff (jq -r '.attempts[0].diff' from the trajectory), with no LLM calls, and report the result.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

From `~/apifix-runs/genai-gptoss-4/trajectory.json` (`openai/gpt-oss-120b` on a hosted endpoint) and a rerun of
the baseline's sandbox with pytest's output printed (the trajectory does not keep it):

- **pytest exit 4 is not a plugin option.** genai's `[tool.pytest.ini_options]` has only
  `asyncio_mode` and `filterwarnings`; there is no `addopts`. Poetry's `dev-dependencies` and
  `group.dev` are already installed (pandas, in `group.dev` only, was in the venv). pytest's
  stderr:

  ```
  ImportError while loading conftest '/work/tests/conftest.py'.
  tests/conftest.py:3: in <module>
      import pandas as pd
  ...
  E   ValueError: numpy.dtype size changed, may indicate binary incompatibility. Expected 96 from C header, got 88 from PyObject
  ```

  A conftest that fails to import is a usage error (exit 4) before any collection, so no JUnit
  report. Cause: poetry.lock has two `numpy` packages, 1.24.4 (`python-versions = ">=3.8"`) and
  1.25.1 (`">=3.9"`), without package markers (Poetry puts the split in pandas' dependency
  markers). `_filter_pins` drops a name pinned to several versions without markers ("numpy
  pinned to several versions; pin not applied"), so pip installed numpy 2.x under the locked
  pandas 1.5.3, which was built against numpy 1.x. Both runs failed the same way.
- **The smoke check.** `typing-extensions==4.7.1` and `tqdm==4.65.0` come from poetry.lock
  (their `pin_sources` are empty: no declared file names them). genai locked openai 0.27.8,
  whose tree has neither httpx nor a new typing-extensions. In the fixed run the lock's
  openai pin was dropped (it conflicts with the new `>=1.0,<2`), but every other lock pin
  stayed. openai releases from 1.40 on need typing-extensions>=4.11, so pip picked openai
  1.39.0, the newest that accepts 4.7.1, and httpx 0.28.1 (nothing pins httpx); openai < 1.55.3
  passes `proxies=` to httpx, which 0.28 removed. Companion pins did not help because pin
  edits only touch declared dependency files, and lockfiles are never edited: a lock pin is a
  constraint, not a declaration. tqdm 4.65.0 is harmless (openai needs `tqdm>4`).
  The model was then told to "fix the dependency file(s)" for pins it cannot edit.
- After the smoke check rejected attempt 1, attempt 2 ran into the hosted provider's daily token limit.
  Without the smoke failure, `sandbox_unavailable` alone would have stopped the job after
  attempt 1 (not retried).

## Plan

1. **Poetry lock entries for several Python versions** (`project._lock_pins`): when one name has
   several versions and no package markers, apply the newest whose `python-versions` allows the
   sandbox's Python (what Poetry's resolver picks for that interpreter), with a note naming the
   choice. `build_install_plan` gets the Python version from the sandbox session. Without a
   Python version, or when none allows it, the old behaviour (pin not applied).
2. **Lock pins that hold the migrated library back** (`project._filter_pins`, fixed run only:
   the session's `LibraryTarget` now carries the migration's companion pins): a lockfile pin
   on a companion package that allows none of the range the target needs is not applied, with
   a note ("poetry.lock: typing-extensions==4.7.1 allows none of >=4.11,<5, which openai>=…
   needs; pin not applied (regenerate the lockfile)"). Same `_excludes` rule as pin edits,
   moved to dependencies.py for both. The baseline keeps every lock pin.
3. **Target pin `>=1.55.3,<2`** (`rules.PIN_SPEC`). Choice: yes. Every openai 1.x below 1.55.3
   breaks at `OpenAI()` with httpx >= 0.28 (released 2024-11-28), and openai's metadata never
   capped httpx below 0.28, so any resolution that lands below 1.55.3 (held back by a lock, a
   companion pin, an old environment) installs a client that cannot be constructed: at runtime
   in production, not only in the sandbox. 1.55.3 has every 1.x API the migration writes (1.x
   only adds), needs Python >= 3.8 (apifix's sandbox range), and is two years old. Cost: a
   project with another package capping openai below 1.55.3 gets an install conflict, which
   the verifier reports as an install failure: loud, instead of a silent runtime break; and a
   project that pins httpx < 0.28 could have used an older 1.x. `first_version` stays 1.0:
   it is about the API (dependency findings, already-migrated, --sandbox-requirements
   upgrades), not about which release to install.
4. **Lock pins in the smoke-check message:** pins that only a lockfile sets get the lockfile as
   their source and the message says so ("from poetry.lock, which apifix does not edit;
   regenerate it"), instead of "fix the dependency file(s)".
5. **pytest exit 4** (`sandbox._test`): when pytest ends without a report, the run keeps the
   error pytest printed (`runner_error`: the conftest ImportError with its last `E` line, or
   `error: unrecognized arguments: ...`, or the last `ERROR:` line; else the last line) and
   the tail of its output (`runner_output`, trajectory only); the reason becomes "pytest
   exited 4 (usage error) without a report: …". In the verifier, an original-code run that
   exited 4 while the fix's run also could not run is `pytest_usage_error` (unverified, an
   environment reason: never sent to the model, not retried) instead of
   `sandbox_unavailable`; the report says so and gives the `apifix verify ...
   --sandbox-requirements FILE` command. A fix whose run alone exits 4 is still a rejection,
   now with pytest's error (it may have broken a conftest).
6. Test dependency groups: already installed (every Poetry group, `dev-dependencies`, PEP 735
   `dependency-groups`, all `optional-dependencies`), so nothing to build there.

Synthetic fixtures for each; then `apifix verify` on attempt 1's diff.

## What was built

As planned, items 1-5. Notes on wording: the lock companion note reads "poetry.lock:
typing-extensions==4.7.1 allows none of >=4.11,<5, which current openai releases need; pin not
applied (regenerate poetry.lock)" (it does not quote the migration's spec, which the fix's own
declaration may differ from). `excludes_range` lives in dependencies.py; pin edits' `_excludes`
calls it.

## Re-check: `apifix verify` on attempt 1's diff (no LLM)

`jq -r '.attempts[0].diff' ~/apifix-runs/genai-gptoss-4/trajectory.json > attempt1.diff`, then
`apifix verify ~/apifix-targets/genai --migration openai-v1 --diff attempt1.diff`. The diff
carries this run's `openai = ">=1.0,<2"` (the old spec); verify uses the diff as is.

- Both runs now start. Baseline: 38 passed, 6 failed (5 need tiktoken's download, no signal;
  `test_build_context_assistance` already failing: `assert 7 == 6`). numpy 1.25.1 applied.
- Fixed run: openai 1.109.1 with httpx 0.28.1, typing-extensions 4.16.0 (the lock's 4.7.1 not
  applied); smoke check passed. 24 of 24 findings resolved, 71% changed-line coverage.
- Verdict: **rejected: regression**, 5 tests (`test_assist_magic_with_fresh_arg`, the four
  `test_custom_exc*`), all `AttributeError: Mock object has no attribute 'completions'`. The
  original patched `openai.ChatCompletion.create` with `autospec=True` (a function); the model
  kept `autospec=True` when it moved the patch to the `genai.generate.OpenAI` class, and an
  autospecced 1.x client has no `chat.completions` (`chat` is a `cached_property`). A real
  defect of attempt 1, which is the feedback attempt 2 should have had.

## Later

- `autospec=True` on a patched client class (`patch("x.OpenAI", autospec=True)`) with
  `.return_value.chat.completions` in the test: a whole-file check, or a line in the mock
  examples and the Tests guide part ("drop autospec when patching the client class").
- The verify outputs keep no failure messages for regressions (they were read by rerunning the
  fixed tree); a few lines per newly failing test in verification.json would help.

## Amendments

### Request: the two "Later" items

Read CLAUDE.md and docs/plans/genai-run-4.md. Build its two “Later” items, recording this request verbatim in that plan’s Amendments section:

autospec on a patched client class: in the mock-target check, reject a new or changed patch(..., autospec=True) (decorator, context manager or mocker.patch) whose target is an openai client class (OpenAI, AsyncOpenAI, AzureOpenAI, …, through any module path) when the test then uses .return_value.chat / .completions / other resource attributes. Message: an autospecced 1.x client has no chat.completions (resources are cached properties), so drop autospec=True when patching the client class, or autospec the method instead. Add a line saying so to the Tests guide part and the mock examples. Replay genai-gptoss-4 attempt 1’s test files: they must be rejected with this message.
Failure messages for regressions: keep a few lines of each newly failing test’s message in verification.json and the trajectory (clipped like other messages) and show the first line in the report, as for no-signal tests.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

### Plan (as built)

1. **Autospec on a patched client class** (`code_checks.autospec_client_problems`, run by
   `mock_target_problems`, so check `mock_targets` in whole-file mode). The migration supplies
   `client_classes` (openai-v1: `OpenAI`, `AsyncOpenAI`, `AzureOpenAI`, `AsyncAzureOpenAI`) and
   `client_resources` (the client's cached properties in openai 1.109.1: `chat`, `completions`,
   `embeddings`, ..., `with_raw_response`). A string-target patch with `autospec=True` whose
   target ends in a client class (any module path), or names one through a repo module's
   import (`from openai import OpenAI as Client`), is an error when the function its mock
   lands in uses a resource on the instance: `<mock>.return_value.chat...`, or through a name
   assigned from `<mock>.return_value` (genai's `client = OpenAI.return_value`). The mock is
   the parameter a decorator fills (lowest first; class decorators per test method; patches
   given `new` fill none), `with ... as m`, `m = mocker.patch(...)`, `m = patch(...).start()`.
   Exempt: `return_value=` on the patch, a scope that sets `<mock>.return_value` itself, and
   uses the original already had (same target and function), so "new or changed" means a
   (target, function) pair the original did not have. One error per target:

   > mock target 'genai.generate.OpenAI' is patched with autospec=True, and test_custom_exc
   > uses OpenAI.return_value.chat.completions (line 28) (and likewise in: ...): an
   > autospecced 1.x client has no chat.completions (resources are cached properties), so drop
   > autospec=True when patching the client class (patch("genai.generate.OpenAI")), or
   > autospec the method instead

   It starts with `mock target '<target>'`, so region edits attribute it to the regions that
   quote the target. Checked against openai 1.109.1: `patch("openai.OpenAI", autospec=True)`
   gives `return_value.chat` (specced on the cached_property object) with no `completions`;
   `patch("openai.resources.chat.completions.Completions.create", autospec=True)` works.
2. **Guide**: a Tests-part bullet ("Drop `autospec=True` when patching a client class ...
   Autospec the method instead (`...Completions.create`, autospec=True)") and a comment line
   in the client-class mock example.
3. **Regression messages** (`TestDelta.regression_messages`): each newly failing / erroring
   test (new tests that fail too) -> the first 5 non-blank lines of the fixed run's message
   (already clipped to 500 characters at parsing); dropped for a test that passes the flaky
   rerun. verification.json and the trajectories carry it (`tests.regression_messages`). The
   report gets "### Tests that now fail" after "Why this verdict": `` `id`: now fails:
   `<first line>` `` (or "now errors"), with the no-signal list's first-line rule (shared
   `_message_head`).
4. Fixture `tests/fixtures/autospec_client` (synthetic, genai-shaped: the tests patch
   `openai.ChatCompletion.create` with `autospec=True`). Tests: the check's forms and
   exemptions, check_candidate, an agent check retry, a pytest run against an openai stub with
   cached-property resources (autospec on the class fails, dropping it or autospeccing the
   method passes), and a Docker test with the real openai 1.x (the autospecced test is a
   regression whose message is `AttributeError: Mock object has no attribute 'completions'`).

### Replay: genai-gptoss-4 attempt 1's test files

The original from `~/apifix-targets/genai` (HEAD), attempt 1's diff applied to a copy, each
test file through `check_candidate` with the job's settings (scanner findings, the
migration's rewrite checks, the fixed copy as the working copy):

- `tests/test_context.py`: rejected (`mock_targets`), "... and test_build_context_assistance
  uses client.chat.completions (line 157): an autospecced 1.x client has no chat.completions
  ...".
- `tests/test_magics.py`: rejected, "test_assist_magic uses mock_client.chat.completions (line
  12) (and likewise in: test_assist_magic_with_args, test_assist_magic_with_fresh_arg)".
- `tests/test_suggestions.py`: rejected, "test_custom_exc uses
  OpenAI.return_value.chat.completions (line 28) (and likewise in:
  test_custom_exc_fallback_on_In, test_custom_exc_long_traceback,
  test_custom_exc_error_inside)".
- `tests/test_generate.py`: ok (it dropped autospec: `@mock.patch("genai.generate.OpenAI")`
  with its own `mock.Mock()` client).

Every one of the 5 regressions `apifix verify` found is in a test named there.
