# pixi / conda dependencies, best-effort installs, incomplete environments

## Request

Context: Read CLAUDE.md. A the hosted provider run on MattOates/djgpt (cloned at ~/apifix-targets/djgpt, MIT license; run at ~/apifix-runs/djgpt-gptoss-1/) shows apifix can’t set up this repo’s environment. [project] dependencies = []; the real dependencies are in [tool.pixi.pypi-dependencies] (openai = "==0.28", openai-whisper, pyaudio, pyttsx3, pocketsphinx), [tool.pixi.dependencies] (conda: typer, spotipy, …), [tool.pixi.target.osx-arm64.dependencies] (pyobjc), and environment.yml (conda, with a pip: section pinning openai==0.27.6). So openai was never installed: the baseline had 0 passed and 3 errors, the fix was rejected by the smoke check (“No module named ‘openai’”) with misleading advice (“raise or drop the pins that conflict”), and attempt 2 was wasted. Separately, attempt 2 aborted on a 413 after the one retry with fewer reply tokens: the retried request was 8,015 against an 8,000 limit, because the hosted provider counted the prompt about 80 tokens higher than the first refusal implied, so the 64-token slack was too small.

Task: record this request verbatim in docs/plans/pixi-conda-and-environment.md, write the plan, and stop for my review. Don’t change code yet.

Read pixi and conda dependencies: treat [tool.pixi.pypi-dependencies] and the pip: section of a conda environment.yml as dependency sources (scanner findings for the openai pin, model edits, the strict well-formedness check, and the sandbox install). Propose how to handle conda-only packages ([tool.pixi.dependencies], the conda list), for example a best-effort pip install of same-named packages except python and known system packages, recorded in the report. Platform-specific sections (target.osx-*) should be skipped on Linux.
Best-effort installs: propose whether apifix should install packages one by one (or retry without failing ones) when the full install fails, so packages that can’t build in the sandbox (e.g. pyaudio needs PortAudio) don’t prevent installing the rest, and how the report would show what was skipped.
Say clearly when the environment is incomplete: when the baseline has no passing tests and its errors are import errors for modules the project declares but that weren’t installed, report a dedicated reason (e.g. environment_incomplete) naming the missing modules and where they’re declared. The verdict should be unverified, the job shouldn’t retry (no edit can help), and the model shouldn’t get “fix the pins” feedback.
Smoke-check message: when the migrated library isn’t installed at all and no dependency file apifix reads declares it, say that, not “raise or drop the pins”.
--sandbox-requirements FILE: a user-supplied requirements file that replaces apifix’s own dependency detection for the sandbox install (both baseline and fix), recorded in the report.
413 slack: make the safety margin proportional, e.g. max(256 tokens, 3% of the limit), and check it against both real refusals from today’s runs.

Use synthetic fixtures for tests. List decisions for me at the end.

## Findings

From `~/apifix-runs/djgpt-gptoss-1/` and the clone (nothing from either is copied into this
repo or its tests):

- **Install plan.** `build_install_plan` read only `pyproject.toml` `[project]` (empty) and
  `[build-system]`, so the sandbox installed hatchling, editables and pytest, and nothing
  else. `dependencies.py` knows `[project]`, `dependency-groups`, Poetry, Pipfile,
  setup.py/cfg and requirements files; it has no pixi or conda support, and
  `is_dependency_file` does not match `environment.yml`, so the scanner did not see either
  openai pin (`==0.28` in `[tool.pixi.pypi-dependencies]`, `==0.27.6` in the environment's
  `pip:` list). The 9 findings were all in Python files.
- **Baseline.** 0 passed, 3 errors, install `ok` in 11 s. The trajectory keeps no test output,
  so which imports failed is not recorded. `djgpt.utils` imports `dotenv` and `rich`, and the
  package imports `openai`, `spotipy`, `typer` and `strenum`; none were installed. Even if test
  output had been kept, `TestCaseResult.message` is clipped to the first 500 characters, and a
  pytest collection error puts `ModuleNotFoundError: No module named 'x'` at the *end* of a
  traceback, so the missing module is usually cut off.
- **Smoke check.** `smoke_check_failed` (reject): "Installed: openai not installed. Fix the
  dependency file(s): raise or drop the pins that conflict." `smoke_reasons` always ends with
  that sentence and does not look at whether anything declares the library. Attempt 1 was
  `rejected`, so the agent retried; nothing the model could edit would have installed openai.
- **The two dependency definitions disagree.** `environment.yml` is an old `conda env export`
  from an osx-arm64 machine: Python 3.10, 130 conda entries with build strings (system
  libraries and transitive packages included) and a `pip:` list of ~180 pins, ~150 of them
  `pyobjc*` (macOS only), plus `numba==0.56.4` / `llvmlite==0.39.1` (no wheels for 3.11).
  `pyproject.toml`'s pixi tables are the current setup (Python >=3.11.8,<3.13, openai ==0.28),
  and pixi does not read `environment.yml`. Installing both together would ask pip for
  `openai==0.28` and `openai==0.27.6` at once, which cannot resolve.
- **Heavy dependency.** `openai-whisper` depends on `torch`; on Linux, PyPI's default torch
  wheel pulls in the CUDA libraries (several GB). Even with correct parsing, a cold-cache djgpt
  install will probably exceed the 900 s install limit or the disk/memory limits. Best-effort
  does not help with that, because the install does not fail, it just takes too long. For this
  repo, `--sandbox-requirements` (item 6) is the practical route.
- **Walker.** `.pixi/` is not in `SKIP_DIRS`, and a conda/pixi environment has no
  `pyvenv.cfg`. In a checkout where `pixi install` has run, the scanner would walk the
  environment's own `lib/python3.x/*.py` (only `site-packages` is skipped). The djgpt clone
  has only `.pixi/.gitkeep`, so it did not matter this time.
- **413 refusals today.** The agent lowers `max_tokens` to `limit - (requested - max_tokens) - 64`.
  Working back from the recorded `max_tokens` / `max_tokens_lowered_from` figures:

  | Refusal | Limit | Requested | max_tokens | Prompt as counted | Old retry (slack 64) | Outcome |
  |---|---|---|---|---|---|---|
  | A: resume-gptoss-1, `backend/testapp.py` (before the retry existed) | 8,000 | 9,265 | 6,000 | 3,265 | (4,671) | job aborted |
  | B: djgpt attempt 2, `src/djgpt/prompt.py` check retry | 8,000 | 8,237 | 6,000 | 2,237 | 5,699 | accepted |
  | C: djgpt attempt 2, next file (`tests/mocks.py` by request order; budget 3,556) | 8,000 | 8,015 | 3,556 | 4,459 | 3,477 | refused: 8,015 again, so the prompt counted as 4,538 (+79) |

  One caveat on C: "Requested" was 8,015 both times, although `max_tokens` dropped by 79.
  That fits the reading in the request (the hosted provider counted the prompt 79 tokens higher on the
  resend). It also fits another reading: the hosted provider's figure did not move with `max_tokens` at all
  for that request. Under the second reading no margin helps, and the retry fails exactly as
  it does today, so the change does no harm.

## Plan

### 1. Reading pixi and conda dependency files (`dependencies.py`)

- **pixi in `pyproject.toml`** (`[tool.pixi.*]`) and a standalone **`pixi.toml`** (same tables
  at the top level):
  - `pypi-dependencies` → `DeclaredDependency` + PEP 508 strings. Values are `"*"`, a PEP 440
    string, or a table (`version`, `extras`; `path` / `git` / `url` / `editable` entries are
    local or VCS code and dropped, like Poetry's). This covers `djgpt = { path = ".", editable
    = true }`.
  - `dependencies` → conda match specs (see the conda bullet below).
  - `feature.<name>.(pypi-)dependencies` are included, like optional dependency groups today.
  - `target.<platform>.…` (also under features): kept only for platforms the sandbox runs on:
    `linux`, `unix`, and `linux-64` (or `linux-aarch64` when Docker's architecture is arm64).
    `osx-*`, `win-*`, `osx`, `win` are skipped, with a note naming the skipped section. The
    scanner still reports findings from every target, because a stale pin is stale on any
    platform.
  - Line numbers: `_locate_key` currently finds the first `key =` anywhere in the file, so it
    is changed to search inside the key's own table. Otherwise `openai` in a pixi table could be
    reported at a `[project]` line.
- **conda `environment.yml` / `environment.yaml`** (any directory for the scanner; the repo
  root only for the install). Runtime stays stdlib-only, so this is a small parser for the
  subset conda environment files use:
  - top-level keys at column 0 (`name`, `channels`, `dependencies`, `variables`, `prefix`);
    block sequences; plain or quoted scalars; `#` comments; one `- pip:` mapping whose items
    are a nested block sequence.
  - Anything else (flow `[...]`, anchors, multi-line scalars, tabs) raises
    `ValueError("unsupported YAML at line N: ...")`. The scanner reports that as a parse error,
    the install skips the file with a note, and whole-file mode rejects an edit that produces it.
  - `pip:` entries are requirement lines, read the way `requirements.txt` lines are (options
    and local references dropped).
  - Conda entries are match specs: `name`, `name=1.2` (conda's `1.2.*`), `name=1.2=build`,
    `name==1.2`, `name>=1,<2`, `name 1.2.*`, `channel::name`. They are converted to PEP 440
    clauses with the build string dropped. Specs we do not understand (`|`, bracket form) become
    unconstrained, with a note.
  - Both kinds produce `DeclaredDependency` entries with lines, so the scanner reports
    `openai==0.27.6` (pip list) and a conda `openai=0.27.6` alike.
- **`is_dependency_file`** gains `pixi.toml`, `environment.yml` and `environment.yaml`. Agent
  tools, whole-file mode, the verifier's scope check and `suspicious.py` (new dependencies) all
  go through this function and `parse_dependency_file`, so model edits to these files are
  allowed and checked with no other changes.
- **`dependency_file_problem`** (the strict check on a model's rewrite), extended to:
  - pixi tables: each `pypi-dependencies` value must be `"*"`, a strict PEP 440 spec, or a
    table with known keys and a valid `version`; each conda value must be a valid match spec.
  - `environment.yml`: the subset parser must accept the file; each `pip:` line must pass the
    same strict check as a requirements line; each conda entry must be a valid match spec.
  - Existing `[project]` / Poetry checks are unchanged.
- **Walker:** `.pixi` joins `SKIP_DIRS`, and a directory that contains `conda-meta/` counts
  as an environment, like `pyvenv.cfg`.
- **Python version:** pixi's `python` dependency and `environment.yml`'s `python=` are added as
  the lowest-priority version sources, after `requires-python`. For djgpt nothing changes
  (`>=3.11` → 3.11).

### 2. What gets installed (`project.build_install_plan`)

Sources in priority order. A name declared by a higher-priority source drops lower-priority
entries for the same name, with a note such as "environment.yml: openai==0.27.6 not installed
(pyproject.toml [tool.pixi.pypi-dependencies] declares openai)".

1. pip sources: `[project]`, dependency groups, Poetry, **pixi `pypi-dependencies`**,
   setup.py/cfg, Pipfile, requirements files.
2. `environment.yml` `pip:` list. This is used only when the repo has **no pixi manifest**:
   pixi does not read `environment.yml`, so when both exist the file is a separate, usually
   older environment (djgpt's case). It still gets scanner findings and can still be edited.
   The note says why it was not installed.
3. **Conda-only packages** (best-effort guesses): pixi `dependencies`, and `environment.yml`'s
   conda list when it is hand-written. A conda list counts as an **export** when the file has
   `prefix:` or any entry carries a build string. An export is a platform snapshot that
   includes system libraries and transitive packages, so it is not installed. A note names it;
   item 5 then names its modules if tests need them.
   - Each guess is pip-installed under the same name, with these exceptions:
     - names in a small rename table are mapped (`pytorch` → `torch`, `redis-py` → `redis`,
       `py-opencv` → `opencv-python`, `msgpack-python` → `msgpack`, `matplotlib-base` →
       `matplotlib`, `pytables` → `tables`);
     - names in a skip list are dropped: `python`, `pip`, known non-Python packages
       (`portaudio`, `ffmpeg`, `openssl`, `ca-certificates`, `nodejs`, `git`, compilers,
       `cudatoolkit`, `mkl`, `tk`, `zlib`, `xz`, `bzip2`, `ncurses`, `readline`, `sqlite`),
       plus the patterns `lib*`, `_*`, `xorg-*`, `font*`, `*-devel`.
   - Version clauses are kept, converted from conda syntax.
4. Tooling, as today: `[build-system] requires`, pytest.

Platform filter (all sources): entries without a marker whose name is known to be
platform-only are skipped on Linux with a note: macOS `pyobjc*` and `appnope`; Windows
`pywin32`, `pywinpty` and `windows-curses`.

`InstallPlan` changes:
- `sources` become section labels, e.g. `pyproject.toml [tool.pixi.pypi-dependencies]`.
- New `guesses`: the conda-derived requirements, kept separate from `requirements`.
- New `declared`: normalised name → where it is declared. Items 4 and 5 use it.

`pixi.lock` is not read (large YAML). It is listed as a known limitation, like `uv.lock`.

### 3. Best-effort installs

Recommendation: drop the failing packages and retry, falling back to one-by-one installs.
This runs inside one install container, as a final step after the existing retry ladder.

- **Ladder:** a strict install on the slim image, then the existing retries (network, full
  image after a build error, dropping lockfile constraints after a resolution error), then a
  **best-effort** run on the last image tried. `max_install_attempts` goes from 4 to 5. Repos
  whose strict install works are unaffected.
- **Best-effort run** (`sandbox_driver.py install --best-effort`, stdlib, Python 3.8+), with
  the manifest gaining `protected.txt`:
  1. Run `pip install -r requirements.txt`. On failure, read the failing distribution names
     from pip's output ("Failed to build X", "Failed building wheel for X", "No matching
     distribution found for X", "Could not find a version that satisfies the requirement X").
     Map each to the top-level requirement that pulled it in, using pip's `Collecting X (from
     Y…)` lines. Drop those requirements and retry, for at most 3 rounds. One joint resolution
     is kept, so versions stay consistent.
  2. When a failure names nothing that can be dropped (e.g. `ResolutionImpossible`), install
     the remaining requirements one at a time into the same venv, in declared order. Each one
     that fails is skipped. Afterwards `pip check` runs and its output goes into the notes:
     installing one at a time loses joint resolution.
  3. Guesses (conda-derived) are installed after the pip dependencies, always best-effort: one
     pip call, then one at a time if that fails. They run with the already-installed versions
     as constraints (`pip freeze` → `-c`), so a guess can never change a declared package.
  4. **Protected** names are never skipped: the migration's distribution (openai), the build
     backend's requirements and pytest. If one of them fails, the install fails as it does
     today, and the message names the package.
- The driver prints one `APIFIX_INSTALL_SKIPPED <json>` line: `[{requirement, source, why,
  output_tail}]`, where `why` is `build_failed`, `no_matching_version`, `conflict`,
  `failed_alone`, `platform` (filtered on Linux), or `not_on_pypi_guess`.
  `SandboxResult.skipped` holds it, together with the plan's filtered entries (platform, skip
  list, export, environment.yml dropped), so the report has one list.
- All of this runs inside the existing install container and its 900 s limit.
- **Report**, under each run's dependency line:
  ```
  - Environment (baseline): installed best-effort; skipped 3 of 14:
    pyaudio (pyproject.toml [tool.pixi.pypi-dependencies]): build failed (portaudio.h not found);
    portaudio ([tool.pixi.dependencies], conda): system package, not on PyPI;
    pyobjc-core ([tool.pixi.target.osx-arm64.dependencies]): macOS only
  ```
  The same list is written to the trajectory (`run_summary`), with the output tails.
- **Verifier:** when the fix's run skipped packages that the baseline installed, it adds an
  `environment_differs` reason (info) naming them. Tests that fail because of those packages
  already show up as regressions.

### 4. Smoke-check message (`verifier.smoke_reasons`)

The sandbox records whether the install plan declares the migrated library
(`SmokeResult.declared_in`, from `InstallPlan.declared`). The verifier compares that with the
baseline's plan. Cases:

| Case | Severity | Message |
|---|---|---|
| Installed, but the check fails | reject (unchanged) | as today, incl. "raise or drop the pins" |
| Not installed; neither the original's nor the fix's files declare it | unverified | "openai is not installed: no dependency file apifix reads declares it (read: pyproject.toml [project], [tool.pixi.pypi-dependencies]; not installed: environment.yml (pixi manifest takes precedence)). If the project declares it elsewhere, pass --sandbox-requirements FILE." |
| Not installed; the original declared it, the fix does not | reject | "the fix's dependency files no longer declare openai (the original declared it in <file>); keep it, e.g. openai>=1" |

The second row is not a dependency-file problem, so it does not make whole-file mode resend
dependency files (`_dependency_problem`).

### 5. `environment_incomplete`

- **Test output:** `test_results` pulls `No module named 'x'` (`ModuleNotFoundError`, or
  `ImportError` with that text) out of the **full** failure text *before* clipping, into a new
  field `TestCaseResult.missing_module`, for JUnit and unittest output.
- **Rule** (verifier, from the baseline run): the baseline's test phase ran, nothing passed,
  and at least one failing or erroring test has a `missing_module` whose top-level name is
  neither stdlib (`sys.stdlib_module_names`) nor a repo module.
- **Reason** `environment_incomplete`, severity `unverified`, listing every missing module with
  where it is declared and what happened to it:
  - "the original code's tests cannot run: they import modules the sandbox environment does
    not have: openai (declared in pyproject.toml [tool.pixi.pypi-dependencies]; not installed:
    the plan ignored it), dotenv (python-dotenv, declared in [tool.pixi.dependencies] (conda);
    skipped: …), whisper (openai-whisper: build failed), foo (not declared in any dependency
    file apifix reads). No edit to the code can fix this; rerun with --sandbox-requirements
    FILE, or check the diff later with apifix verify."
  - Modules map to distributions through normalised names plus a small import-name table
    (`dotenv` → `python-dotenv`, `yaml` → `pyyaml`, `PIL` → `pillow`, `cv2` → `opencv-python`,
    `sklearn` → `scikit-learn`, `bs4` → `beautifulsoup4`, `speech_recognition` →
    `speechrecognition`, `whisper` → `openai-whisper`, `dateutil` → `python-dateutil`, `jwt`
    → `pyjwt`, `attr` → `attrs`, …).
- **Agent:** `environment_incomplete` joins the never-retried set in `_retryable`, even when
  another reason rejects (the next attempt could not be verified either). Attempt 1 still runs,
  so the user gets a diff to check later. `feedback_lines` leaves this reason out, and so does
  the smoke "not declared" message, so the model never gets dependency advice about the
  environment.
- **Report:** the reason appears near the top with the module list, and the suggested
  `apifix verify … --sandbox-requirements FILE` command is printed.
- When the baseline has passing tests but some tests fail on missing modules, an
  info reason `missing_modules` lists them. It does not affect the verdict.

### 6. `--sandbox-requirements FILE` (`fix` and `verify`)

- The file replaces the repo's declared dependencies, the lockfile constraints and the
  conda guesses for both the baseline and the fixed run. apifix still adds its own tooling
  (build backend requirements, pytest), because the repo is installed offline in the test phase.
- **The fix's dependency edits still count.** For the fixed run, any name whose declaration
  the fix changed or added in the repo's dependency files (compared with the original's
  `DeclaredDependency` set) takes the fix's declaration instead of the user file's line.
  Example: the user's `openai==0.28` becomes the fix's `openai>=1.0`. Without this, the fixed
  run would test the new code against the old library. The report lists each replaced line.
- **Validation at startup:** the file must pass the strict requirements check. Rejected:
  `-r`/`-c`/`-e`, local paths, and URLs carrying credentials (`user:token@`), because the
  manifest is mounted in the networked install container. Allowed options: `--index-url` and
  `--extra-index-url` without credentials, for cases like torch's CPU-only index.
- **Recorded:** the report line "Dependencies: from --sandbox-requirements <name> (N
  requirements, sha256 …), replacing detection" plus the replaced lines. The trajectory gets
  the path, hash and contents (the user's own file, small).
- **Plumbing:** `SandboxConfig.requirements_override` → `build_install_plan(...,
  override=..., original_declared=...)`. `verify_diff` passes it through too.

### 7. 413 margin (`agent._fitting_max_tokens`)

`TOKEN_LIMIT_SLACK = 64` is replaced with `token_limit_margin(limit) = max(256,
ceil(0.03 * limit))`: 256 at 8,000, 900 at 30,000. Still one retry per request. Checked
against today's refusals:

| Refusal | New retry max_tokens | Fits? |
|---|---|---|
| A (prompt 3,265; file needs ~1,833) | 8,000 − 3,265 − 256 = **4,479** | 7,744 by the first count; enough room for the file |
| B (prompt 2,237; file needs ~2,660) | 8,000 − 2,237 − 256 = **5,507** | 7,744 (the old 5,699 was accepted, so this is too) |
| C (prompt 4,459, recounted as 4,538; file needs ~754) | 8,000 − 4,459 − 256 = **3,285** | 4,538 + 3,285 = 7,823, 177 under the limit. The old retry was 15 over |

If the hosted provider's figure for C really did not move with `max_tokens` (the caveat above), the retry
still fails, with the same message as today.

### Tests (synthetic fixtures only)

- New fixtures: `tests/fixtures/pixi_repo/` (pyproject with `[project] dependencies = []`, pixi
  pypi deps incl. `openai = "==0.28"`, a self `path` dep, conda deps, a `target.osx-arm64` table,
  a feature) and `tests/fixtures/conda_env_repo/` (hand-written `environment.yml` with a `pip:`
  list pinning `openai==0.27.6`). Exported-style and malformed YAML snippets inline in unit tests.
- `test_dependencies`: pixi tables, targets, features, `pixi.toml`, line numbers inside the right
  table; YAML subset (comments, quotes, `pip:`, unsupported constructs → errors with lines);
  conda spec conversion; strict checks (valid/invalid edits for each format).
- `test_scanner`: openai pin findings in both fixtures, at the right lines; `.pixi`/`conda-meta`
  dirs skipped.
- `test_project`: priority and dropped-duplicate notes, environment.yml ignored next to pixi,
  export detection, rename/skip lists, platform filter, `declared`, python from pixi; override
  plans incl. fix-changed names replaced.
- Driver: best-effort logic against a fake pip (canned synthetic pip outputs): name extraction,
  `Collecting X (from Y)` mapping, drop rounds, one-by-one fallback, guesses with constraints,
  protected failure, `APIFIX_INSTALL_SKIPPED` line.
- `test_sandbox` (fake Docker): ladder ends in best-effort, `skipped` merged with plan filters,
  override manifest.
- `test_test_results`: `missing_module` from a JUnit collection error whose module line is past
  500 characters; unittest output.
- `test_verifier`: `environment_incomplete` (declared / skipped / undeclared modules, repo and
  stdlib modules excluded, a passing baseline gives `missing_modules` instead), smoke-message
  cases, `environment_differs`, feedback without these reasons.
- `test_agent`: no retry with `environment_incomplete` even alongside a rejection; margin table
  A/B/C as numbers.
- `test_cli`: `--sandbox-requirements` validation errors, report and trajectory lines, `verify`.
- Docker: a synthetic pixi repo whose pypi deps are `six` and a name that does not exist on
  PyPI (skipped: no matching version), whose tests import the missing one →
  `environment_incomplete`; the same repo with `--sandbox-requirements`. No real build failure
  (pyaudio) in CI: it would depend on PyPI's sdist and the image's headers.

### Docs

- CLAUDE.md: layout line for `dependencies.py` (pixi / conda), known limitations: YAML subset,
  exports not installed, environment.yml ignored next to pixi, rename/skip lists are partial,
  one-at-a-time install loses joint resolution, `pixi.lock` not read, platform targets,
  `environment_incomplete` rule, override semantics, the margin.
- README: dependency sources, best-effort installs, `--sandbox-requirements` with an example.

### Afterwards

Rerun djgpt on a hosted endpoint. Expected: the baseline still cannot import `whisper`, and possibly others,
unless torch installs in time, so I expect a clean `environment_incomplete` stop after one
attempt instead of a wasted retry. Then `apifix verify` on the diff with a small
`--sandbox-requirements` file (openai==0.28, typer, spotipy, python-dotenv, rich, strenum,
pytest, plus the torch CPU index if whisper is needed) to get a real verdict.

## Decisions for you

1. **environment.yml next to a pixi manifest:** I'd skip it for the install (it still gets
   findings and can be edited), since pixi never reads it. Alternative: install both, with
   pixi winning name clashes. For djgpt that would mean ~30 stale pins, numba and llvmlite
   failing to build, and the pyobjc list filtered out.
2. **Exported conda lists** (`prefix:` or build strings): not installed, only named when tests
   need them. Alternative: treat the exported Python packages as constraints, like a lockfile.
3. **Conda-only packages:** best-effort pip install under the same name, with the rename and
   skip lists, after the pip deps and constrained to them. OK, or skip conda-only packages
   entirely and just report them?
4. **Best-effort strategy:** drop the failing packages and retry (3 rounds), then one at a time,
   as the last rung of the ladder, with openai, the build backend and pytest protected. OK?
   And should `environment_differs` be info (my proposal) or weak?
5. **`environment_incomplete` trigger:** nothing passes and *at least one* test fails on a
   missing third-party module, whether declared or not (my proposal), versus *every*
   failure being such an import error. Also: still run attempt 1 for the diff (my proposal), or
   stop before any LLM call once the baseline shows this?
6. **Smoke check when nothing declares the library:** `unverified` (my proposal) or keep
   `reject`?
7. **`--sandbox-requirements`:** for the fixed run, take the fix's declarations for every name
   the fix changed (my proposal), versus only the migrated library, versus using the file
   unchanged. Allow `--index-url` / `--extra-index-url` (without credentials)?
8. **Margin:** `max(256, ceil(3% of limit))` with one retry, as asked. A second lowered retry
   when the overshoot is small is possible but not proposed.
9. **Scope extras:** standalone `pixi.toml` support and the `.pixi` / `conda-meta` walker skip
   are included because they are cheap. Drop either?
10. **Platforms:** keep `linux`, `unix` and the matching `linux-*` targets, plus a small
    built-in list of macOS/Windows-only pip names. OK?

## Amendments

Approved with reduced scope. Record these decisions verbatim in the plan’s Amendments section, then build phases 1 and 2 only.

Phase 1: item 4 (smoke-check messages), item 5 (environment_incomplete, including missing_module extracted from the full text before clipping), item 6 (--sandbox-requirements for fix and verify, taking the fix’s declaration for every name the fix changed; --index-url / --extra-index-url allowed without credentials), item 7 (margin max(256, ceil(3% of limit)), one retry), and the .pixi / conda-meta walker skip.

Phase 2: item 1 limited to pixi pypi-dependencies (in pyproject.toml and pixi.toml, with features and Linux targets as planned, _locate_key searching the right table) and the pip: section of environment.yml / environment.yaml (the YAML-subset parser as planned). Scanner findings, is_dependency_file, the strict well-formedness check, and the install plan for these. environment.yml is not installed when a pixi manifest exists (decision 1); exported conda lists are not installed (decision 2). The platform filter for known macOS/Windows-only pip names applies.

Deferred (phase 3, don’t build): conda-only package guesses and best-effort installs. In the report, list conda-only packages as “not installed (conda-only package; not supported yet)”, and mention them in environment_incomplete when a missing module maps to one.

Decisions 5, 6, 8, 9 and 10: as proposed. Use synthetic fixtures only. Run the fast suite and ruff, and the Docker suite if the network allows, then commit.

## Built (phases 1 and 2)

- Phase 1: `TestCaseResult.missing_module` (JUnit and unittest, read from the full text);
  `environment_incomplete` / `missing_modules` (verifier) and no retry on it (agent);
  `library_not_declared` / `library_removed` smoke-check reasons; environment reasons are left
  out of model feedback; `--sandbox-requirements` for `fix` and `verify` (`SandboxConfig.
  requirements_override`, `Sandbox.session(..., original=)`), with the fix's declarations for
  every name it changed, the report line and the trajectory record; margin
  `max(256, ceil(3% of limit))`; walker skips `.pixi` and dirs holding `conda-meta/`.
- Phase 2: pixi `pypi-dependencies` (pyproject.toml and pixi.toml, features, Linux targets,
  `_locate_key` inside the entry's own table, Poetry tables too), the `pip:` list of
  `environment.yml` / `.yaml` (YAML-subset parser), strict checks for both, the install plan
  (environment.yml yields to a pixi manifest and to other sources; exported conda lists and
  conda-only packages are listed as not installed; macOS/Windows-only pip names filtered),
  the "Declared dependencies not installed" report section, scope checks that treat pixi
  pypi-dependencies edits as dependency changes, `yaml`/`toml` fences in whole-file prompts.
- Not built (phase 3): conda-only package guesses, best-effort installs.

Checked read-only on the djgpt clone (no sandbox, no LLM): both openai pins are now findings
(`pyproject.toml:44`, `environment.yml:158`); the install plan is openai==0.28, openai-whisper,
pyaudio, pyttsx3, pocketsphinx, rich>=13.0.0 (+ hatchling, editables, pytest), with 10
conda-only packages, 2 osx-arm64 entries and environment.yml (pixi takes precedence) listed as
not installed. Without best-effort installs, pyaudio (needs PortAudio headers) will still fail
the whole install there, and openai-whisper pulls in torch; `--sandbox-requirements` is the
way to get a verdict for djgpt until phase 3.
