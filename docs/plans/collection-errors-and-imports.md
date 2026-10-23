# Collection errors, undeclared imports

## Request

Context: Read CLAUDE.md. apifix verify on HCAI-Lab-GT/SuspensePerception (~/apifix-targets/suspense, latest report at ~/apifix-runs/suspense-verify-2/) is unverified because tests/test_thriller.py imports src/thriller/adversarial.py, which needs undeclared heavy packages (torch, textattack, nlpaug, nltk, vaderSentiment, plus a spaCy model). pytest stops the whole session on that one collection error, so the other four test files (which cover the migrated code) never run, on either side.

Task: record this request verbatim in docs/plans/collection-errors-and-imports.md, then build it.

Keep going past collection errors: run pytest with --continue-on-collection-errors (unless the repo’s own config conflicts), so other test files still run. A module that fails to collect counts as an error for each run, and the baseline comparison treats “fails to collect before and after” like any already-failing test (no signal, not a regression). environment_incomplete should apply only when nothing usable runs; when other tests pass, report the missing modules as info with the files they block.
Undeclared imports, all at once: before the first sandbox run, statically collect the top-level third-party imports in non-test and test code (excluding stdlib, the repo’s own modules and packages it declares or that --sandbox-requirements lists), map import names to likely pip names where they differ (e.g. yaml→pyyaml, dotenv→python-dotenv, sklearn→scikit-learn, cv2→opencv-python, vaderSentiment→vadersentiment), and list them in the report as “imported but not declared”, with the files that import them. Information only; don’t install anything automatically.

Synthetic fixtures only. Run the fast suite, Docker if the network allows, and ruff, then commit.

## Findings

- `PYTEST_COMMAND` has no `--continue-on-collection-errors`. pytest ends the session after
  collection when any module failed to collect ("Interrupted: 1 error during collection"),
  so suspense-verify-2 has exactly one test case per side: the collection error
  `tests.test_thriller` (0 / 0 / 1 / 0 on both).
- The JUnit parser already records a collection error as one `error` case with the module as
  its id (no `::`), and counts it in `errors`.
- `compare_runs` skips a module that fails to collect on both sides: it lands in no bucket
  (not `no_signal_preexisting`, not anywhere), so the report says nothing about it. A module
  broken only before the fix is skipped too.
- `environment_reasons` decides `environment_incomplete` by "no baseline test passed". With
  one collection error and nothing else run, that was correct here; once the other files
  run, a baseline with no pass but real failures (an assertion on the 0.x code) is still
  something a fix can change, so "nothing usable" should mean: every baseline test that ran
  is blocked by a missing module.
- In SuspensePerception, spacy, nltk, vaderSentiment (`nlp` extra) and torch (`ml` extra) are
  declared in pyproject.toml's optional dependencies; textattack and nlpaug are only in a
  commented-out `adversarial` extra. The user's --sandbox-requirements lists none of them.
  So by the request's definition (declared by the repo or listed by the file), the
  undeclared ones there are textattack and nlpaug.

## Plan

### 1. pytest keeps going past collection errors

- `detect_tests` adds `--continue-on-collection-errors` to the pytest command (both forms:
  explicit file list and the repo's own collection).
- Conflict: the repo's pytest config `addopts` stops the session at the first failure
  (`-x` / `--exitfirst`, also inside short-option bundles such as `-xvs`; `--maxfail N` with
  N > 0; `--sw` / `--stepwise`). pytest would then stop after the first collection error
  anyway, so the flag is not added and the test plan notes why ("pyproject.toml addopts
  stops at the first failure (-x): collection errors still end the session"). `addopts` is
  read like `testpaths` (pytest.ini, pyproject `[tool.pytest.ini_options]`, tox.ini,
  setup.cfg), split with `shlex`.
- Each module that fails to collect stays one `error` case per run (as parsed today).
- `compare_runs`: a module that fails to collect before and after goes to
  `no_signal_preexisting` (or `no_signal_external` when either message has a
  network/credential marker), so it is reported as already failing, never as a regression.
  A module that failed to collect only before the fix goes to `fixed_by_change`; its tests
  are new tests, as today. A module broken only after the fix stays `collection_error`
  (reject).

### 2. environment_incomplete only when nothing usable runs

- A baseline test is *blocked* when it failed or errored with "No module named 'x'" for a
  module that is not stdlib and not the repo's own. `environment_incomplete` (unverified,
  not retried) when the baseline has blocked tests and no other test outcome: nothing
  passed, and nothing failed for another reason. Otherwise `missing_modules` (info) as now,
  with the files they block: "... nlpaug (not declared in any dependency file apifix
  reads), blocking tests/test_thriller.py; the other tests ran". Test ids map to files by
  their module part (`tests.test_thriller` -> `tests/test_thriller.py` when that file
  exists in the original repo, else the dotted name).

### 3. Imported but not declared (`engine/imports.py`, new; stdlib only)

- `undeclared_imports(root, override=None) -> list[UndeclaredImport]` (module, likely pip
  name, files), computed once per job from the original repo before the first sandbox run
  (`fix` before the baseline; `apifix verify` before its baseline).
- Every `.py` file the walker yields (non-test and test code alike), except under top-level
  `docs/` / `doc/` (Sphinx `conf.py` imports themes no one installs to run tests). Absolute
  imports anywhere in the file, by first name. Not counted: relative imports, imports inside
  `if TYPE_CHECKING:`, and imports inside a `try` whose handlers catch `ImportError`,
  `ModuleNotFoundError`, `Exception` or everything (optional dependencies). Files that do not
  parse are skipped.
- Excluded names: stdlib (`sys.stdlib_module_names`, plus `__future__`), the repo's own
  modules (`repo_module_names`: every `.py` stem and every dir holding Python files), tools
  apifix or the build always installs (pytest, `_pytest`, setuptools, `pkg_resources`,
  wheel, pip), and every name declared anywhere: all dependency files the walker finds
  (`parse_dependency_file`: every table, extras and groups included), conda entries the
  install plan lists as not installed (pixi `dependencies`, an environment's conda list),
  the build-system requirements, and the --sandbox-requirements lines.
- Import name -> pip name: `distribution_candidates` (the existing table, extended:
  `skimage` -> scikit-image, `Bio` -> biopython, `fitz` -> pymupdf, `zmq` -> pyzmq,
  `MySQLdb` -> mysqlclient, `psycopg2` -> psycopg2 / psycopg2-binary, `dns` -> dnspython,
  `nacl` -> pynacl, `github` -> pygithub, `jose` -> python-jose, `multipart` ->
  python-multipart, `slugify` -> python-slugify, `telegram` -> python-telegram-bot, `kafka`
  -> kafka-python, `faiss` -> faiss-cpu, `mpl_toolkits` -> matplotlib, `google` /
  `azure` namespaces). Names are compared normalised, so `vaderSentiment` matches a
  `vadersentiment` declaration and is shown as `vadersentiment`. A name is declared when
  any candidate is; namespace packages (`google`, `azure`) when any declared name starts
  with `google-` / `azure-`.
- Report (fix and verify), a section before the verification:
  `## Imported but not declared` / "Found by reading the code before any sandbox run;
  information only: nothing was installed because of it. To install them in the sandbox,
  list them in a --sandbox-requirements file." / `- `nlpaug` (pip: `nlpaug`):
  src/thriller/adversarial.py` (at most 5 files each, "+N more"). Trajectory:
  `job.undeclared_imports` (module, distribution, files).
- Never installed, never sent to the model, no effect on the verdict.

### Tests (synthetic fixtures)

- `tests/fixtures/collection_errors/`: a package whose `heavy.py` imports undeclared
  `nlpaug` / `textattack` and declared `torch` (extra), a test file importing it, two test
  files covering the migrated code, `yaml` / `dotenv` / `sklearn` / `cv2` /
  `vaderSentiment` imports with some declared, an optional import in `try/except
  ImportError`, a `TYPE_CHECKING` import, a `docs/conf.py`.
- Unit tests: the flag and each conflict form; `compare_runs` buckets for collection errors;
  `environment_reasons` (blocked only -> incomplete; blocked plus passes or real failures ->
  info with files); `undeclared_imports` exclusions, mapping, override; report lines and
  trajectory in fix and verify; a Docker integration test where one test file fails to
  collect and the others still run.

## Later

- spaCy models (`spacy.load("en_core_web_sm")`) and `nltk.download(...)` data: a model is a
  pip package that is never imported, and nltk data needs the network at test time. Both
  could be listed next to the undeclared imports.

## Built

As planned, with these details:

- `project.py`: `CONTINUE_ON_COLLECTION_ERRORS`, `stops_at_first_failure` (addopts read with
  `shlex`; short-option bundles stop at a letter that takes a value, so `-rxXs` and
  `-pno:xdist` do not count as `-x`).
- `verifier.compare_runs`: collection errors on both sides -> `no_signal_preexisting` /
  `no_signal_external`; before only -> `fixed_by_change` only when tests of that module ran
  after the fix (a module the fix deleted is left to the tampering checks).
- `environment_reasons`: "usable" excludes import errors of any kind (also of stdlib or repo
  modules, which are not listed as missing) and network/credentials failures.
- `imports.py` (new); `repo_module_names` moved to walk.py; `IMPORT_DISTRIBUTIONS` extended
  (also `hydra` -> hydra-core, found by trying the listing on SuspensePerception);
  `verifier.requirements_override`.
- Fixture `tests/fixtures/collection_errors/`; tests in test_project, test_verifier,
  test_imports (new), test_verify_diff, test_agent; Docker test
  tests/integration/test_collection_errors_docker.py (a real pytest run: 2 passed, 1 error,
  then verify: no signal, `missing_modules` naming the file).
- README and CLAUDE.md.

The listing on the real SuspensePerception checkout (read-only, with and without the user's
--sandbox-requirements file): textattack and nlpaug (src/thriller/adversarial.py), plus PIL,
ftfy, hydra, mpi4py and omegaconf in scripts/ and experiments_archive/. torch, nltk, spacy
and vaderSentiment are declared in pyproject.toml's optional extras, so they are not listed.

Fast suite 1308 passed; `pytest -q --docker` 1329 passed (no skips); ruff clean. Not rerun
live on SuspensePerception.
