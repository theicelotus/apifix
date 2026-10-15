# Plan: first real repo (resume-analyzer): scanner gaps, test layouts, whole-file blockers

Requested 2026-10-15. Investigation only; this plan is for review, nothing is built yet. The
request is verbatim.

## Request

Context: Read CLAUDE.md. I’m testing apifix on its first real repo, cloned at
~/apifix-targets/resume-analyzer (brianAbell/AI-powered-Resume-Analyzer-and-Feedback-Generator,
openai==0.28.1, Flask). apifix scan found 3 findings in backend/app.py and the pin in
requirements.txt, but nothing in backend/testapp.py, which has
@patch('app.openai.ChatCompletion.create').

Task: investigate and write a plan in docs/plans/first-real-repo.md (request verbatim), then stop
for my review. Don’t change code yet.

Scanner gap: find out why that patch target isn’t flagged, and propose a fix so string targets
that end in an old API path with any module prefix (app.openai.ChatCompletion.create,
src.x.y.openai.ChatCompletion.create) are found. Check whether the dict-response rule also misses
things in this repo’s tests.
Running the tests: find out whether apifix’s sandbox can get a passing baseline on the original
code. Things I expect to be problems: the test file is named testapp.py (not test_*.py); tests
and app live in backend/ while requirements.txt is at the root; and tests import from app import
.... Report what actually happens, and propose how apifix should handle layouts like this (e.g.
read the repo’s pytest config, fall back to unittest discovery, run from the tests’ directory),
with the trade-offs.
List anything else in this repo that would stop a whole-file run from succeeding.

This repo has no license: don’t copy its code into apifix’s repo. Any test fixtures must be
small synthetic reproductions of the patterns.

## The repo, briefly

```
requirements.txt        29 exact pins (openai==0.28.1, typing_extensions==4.5.0, Flask==2.2.3, ...)
backend/__init__.py     empty
backend/app.py          Flask app; module-level openai.api_key; openai.ChatCompletion.create;
                        response['choices'][0]['message']['content']; logging.info to stderr
backend/testapp.py      unittest.TestCase, 8 tests; `from app import app, process_file`;
                        @patch('app.openai.ChatCompletion.create') x2 with dict return values;
                        4 tests open resumes/*.pdf (relative to the cwd)
.gitignore              ignores resumes/   (the PDFs were never committed)
```

No pytest config, no setup.py/pyproject.toml, no CI workflows, no test instructions in the
README.

## Findings

### 1. Why the patch target is not flagged

`_Visitor.visit_Constant` (`migrations/openai_v1/python_checks.py:242`) only looks at strings
that **start with** `openai.`. `'app.openai.ChatCompletion.create'` starts with `app.`, so it is
skipped. The file name does not matter here: the same file scanned as `backend/test_app.py`
still has no patch finding.

### 2. The dict-response rule misses this repo's tests too, for a different reason

Both mock return values (`{'choices': [{'message': {'content': ...}}]}`, lines 96 and 109) have
exactly the shape the rule looks for. They are missed only because the rule runs in test files
only, and `is_test_file('backend/testapp.py')` is False. Checked: scanning the same source as
`backend/test_app.py` yields both findings (`response_access [likely]` at 96 and 109).

`is_test_file` (`tampering.py:92`) = `TEST_FILE_RE` (`project.py:213`,
`^(test_.*|.*_test|tests)\.py$`) or `conftest.py` or a `tests/`/`test/`/`testing/` directory.
`testapp.py` matches none of them. **This is more serious than a scanner miss**, because the
same function decides:

| Where | Effect for `backend/testapp.py` today |
|---|---|
| `verifier._test_edits` | **no tampering check at all**: an edit that deletes or weakens its assertions would not be caught (hard rule) |
| `verifier.changed_line_coverage` | counted as source code: changed test lines count as "unexecuted" |
| `whole_file` (ordering, read-only context, mock-target checks) | treated as a source file: gets no context, no mock checks |
| `guide_parts` | the `tests` guide part is not selected |
| `project.detect_tests` | not found: **"no test files found"** |

### 3. What the sandbox actually does with the original code

As shipped: `detect_tests` finds no test files, so `SandboxSession._prepare` returns
`unverified / "no test files found"` before Docker starts. `apifix fix` on this repo can never
verify anything.

To see what each candidate layout would do, I ran the real sandbox on the unmodified repo with
`detect_tests` patched to fixed commands. One install (python:3.11-slim, chosen by default; all
29 pins installed on the first try), then:

| # | Command (cwd `/work` = repo root) | Result |
|---|---|---|
| a | `pytest` (apifix's default command) | exit 5, **no tests collected** (pytest's default `python_files` is `test_*.py *_test.py`) |
| b | `pytest backend/testapp.py` | collection **error**: `ModuleNotFoundError: No module named 'app'`. `backend/` has an `__init__.py`, so pytest imports `backend.testapp` and puts the repo root, not `backend/`, on `sys.path` |
| c | `pytest -o pythonpath=backend backend/testapp.py` | **runs**: 4 passed, 4 failed |
| d | `unittest discover -v` (root; default pattern `test*.py` *does* match `testapp.py`) | **error**: imports `backend.testapp`, same `ModuleNotFoundError: app` |
| e | `unittest discover -s backend -v` | **runs**: 4 passed, 4 errors (but apifix parsed it wrong, see below) |

In c and e the 4 non-passing tests are the same, and they are not about layout:
`FileNotFoundError: 'resumes/resume.pdf'` (and `no_text_resume.pdf`, `too_large_file.pdf`). The
PDFs are gitignored and were never committed, so **no clean clone can pass these four tests, in
any layout or cwd**. The four that pass are the two home-route tests and both
`resume-feedback` tests, which are the ones that exercise the OpenAI code. So the best
reachable baseline is 4 passed / 4 failing. The verifier already treats tests that fail before
and after the fix as `no_signal_preexisting` ("not blamed on the fix"), so that baseline is
usable.

Two unittest-parsing bugs showed up in run e (`test_results.py`):

- **A test was dropped.** `test_resume_feedback_valid` (the main OpenAI test) printed
  `... 2026-10-15 ... INFO - Successfully obtained feedback ...` on stderr between `... ` and
  `ok`, so `_UNITTEST_LINE` never matched it. The summary counts say 8 tests; the per-test list
  has 7. The verifier compares per-test results, so this is exactly the test whose regression it
  would miss.
- **Failure messages are always empty under `-v` when a test has a docstring.** `-v` prints the
  docstring's first line after `ERROR: name (id)`, and `_UNITTEST_BLOCK` requires the `----`
  line to come right after it.

Minor: the sandbox note says "offline editable install failed; used PYTHONPATH instead" even
when there was nothing to install (no setup.py/pyproject.toml; the driver's own log says so).

### 4. Other things that would stop a whole-file run

Checked in throwaway containers with openai 1.x:

1. **testapp.py is never sent.** Whole-file mode only sends files with findings, and it has none
   (findings 1 and 2). The fix can't update the patch targets or the dict mocks.
2. **The old patch target still "works" after migration.** Under openai 1.109.1,
   `patch('app.openai.ChatCompletion.create')` succeeds, because 1.x keeps a
   `ChatCompletion` proxy that only raises when it is called. So after `app.py` is migrated:
   `test_resume_feedback_valid` calls the real client (fails), and
   `test_resume_feedback_invalid` **keeps passing while testing nothing** (`assert_not_called()`
   on a mock no code can reach). Once finding 1 is fixed, a stale target left in the fixed tree
   stays a scanner finding, so the verifier's completeness check rejects it.
3. **No API key in the sandbox.** The repo has no CI workflows, so the `OPENAI_API_KEY`
   placeholder is not applied (hard rule: only variables the repo's own CI sets). Checked:
   `openai.OpenAI()` without a key raises `OpenAIError` immediately. A module-level
   `client = OpenAI()` in `app.py`, the most natural migration, breaks the import, so all 8 tests
   error, even with correct mocks. The existing `failure_hints` entry explains the error after
   the fact; nothing tells the model up front.
4. **The dependency bump can't produce a working install as-is.** `requirements.txt` is fully
   pinned, so apifix uses it as a lockfile/constraints. Its pin `typing_extensions==4.5.0`
   stays. Checked with pip: changing only `openai==0.28.1` to `openai>=1,<2` resolves to
   **openai 1.5.0**, pydantic 1.10.26 and httpx 0.28.1, and then
   `openai.OpenAI(api_key="x")` raises `TypeError: Client.__init__() got an unexpected keyword
   argument 'proxies'`. Older openai releases (before ~1.55.3) are incompatible with httpx 0.28.
   pip succeeds, so no install-error feedback reaches the model. Every test that builds a client
   fails with a TypeError that doesn't mention dependencies.
5. **Mock-target checks and suggestions can't see `backend/app.py`.** `code_checks.MODULE_ROOTS`
   is `("", "src/")`, so `app` in `'app.client...'` maps to no repo module. The check is skipped
   ("outside the repo") and no "patch this target" suggestion is produced, though this is
   exactly the case those suggestions were built for.

Not blockers: both files fit the 24,000-byte budget (4.2k + 5.5k). Python 3.11 is a fine default
(aiohttp 3.8.5 would have no wheels on 3.12+, but 3.11 is what gets picked).

## Proposed changes

Ordered by dependency. Each is a separate, tested change.

### A. One definition of "test file", name or content (fixes 2, part of 4.1)

`is_test_file(relpath, source=None)`:

- **Name**: the union of pytest's and unittest's defaults: `test_*.py`, `*_test.py`,
  `test*.py` (unittest's `discover` default, which covers `testapp.py`, `tests.py`,
  `testing_utils.py`), `conftest.py`, plus the existing `tests/test/testing` directories. A
  repo's pytest `python_files` (pytest.ini / tox.ini / setup.cfg / pyproject) is added when set.
- **Content** (when source is available): a module that subclasses `unittest.TestCase` /
  `IsolatedAsyncioTestCase`, or imports pytest and defines a top-level `test_*` function.
- Callers that only have a path (the scanner has the source, the verifier reads both versions)
  pass the source where they have it. The verifier uses the **original** file's source, so an
  edit can't make a test file stop counting as one.

Trade-off: over-inclusion (e.g. a `testdata.py` helper) only means more tampering checks and a
test-file prompt, which is the safe direction. Under-inclusion silently turns off the hard rule,
so the definition leans toward inclusion. `TEST_FILE_RE` in `project.py` and `is_test_file` stop
being separate definitions.

### B. String references with any module prefix (fixes 1)

In `visit_Constant`: if the string is a dotted path of identifiers containing an `openai`
segment, classify the part from the **first** `openai` segment on
(`app.openai.ChatCompletion.create` -> `openai.ChatCompletion.create`;
`src.x.y.openai.error.RateLimitError` -> `openai.error.RateLimitError`). Same categories as
today (`api_call`, `error_class`), `[likely]`.

- Guard: skip when the prefix names a repo-local module called `openai` (`app/openai.py`,
  `pkg/openai/`). This is checked through the scan root, so the scanner needs one cheap set of
  repo module paths. It stays pure stdlib.
- The suggestion says the prefix-aware thing: "this patches `openai.ChatCompletion.create` as
  seen from module `app`; in 1.x patch the client `app` uses, e.g. `app.client.chat.completions.create`
  or `app.OpenAI`". The module name comes from the prefix.
- `rules.py` stays data: the generic "find the migrated module's segment" logic goes through
  `Migration.api_modules`, so other migrations get it too.
- Non-goal (kept as a known limitation): `patch.object(app.openai.ChatCompletion, "create")`.
  The scanner doesn't resolve repo-module attributes.

### C. Running tests in layouts like this one (fixes 3)

Options considered:

| Option | Here | Trade-offs |
|---|---|---|
| Read the repo's pytest config (`testpaths`, `python_files`, `pythonpath`, `rootdir`) | No config, so it changes nothing here | Cheap, and the repo's own intent. Only helps repos that have config; adding it is right regardless |
| Fall back to unittest discovery | Works only with `-s backend` (run e); from the root it fails like pytest (run d) | Needs the right start dir anyway. Text output is fragile (two parser bugs found). Several test dirs mean several runs. Test IDs differ from pytest's, which matters if the runner choice changes between baseline and fix |
| Run from the tests' directory (cwd = `backend/`) | Would fix the imports | Changes what relative paths in tests mean (`resumes/...` here; unknowable which cwd the author used). Two dirs mean two cwds. The driver, coverage paths and test IDs all assume `/work` |
| **Add the tests' import roots to `sys.path`, keep cwd at the repo root** (recommended) | Run c: works, same results as e | One static guess per test dir (below). Keeps cwd, JUnit XML, coverage and test IDs as today. Doesn't help tests that need a specific cwd, but nothing static can know that |

Recommended design:

1. `detect_tests` uses definition A to find test files, and **passes them to pytest
   explicitly** (pytest collects explicitly named files whatever their name, as run c showed).
   pytest stays the default runner, since it runs `unittest.TestCase` tests and reports JUnit
   XML. unittest stays only for repos where pytest isn't usable (e.g. `load_tests` protocol
   modules), with the parser fixes in item 3 below either way.
2. **Import roots.** For each test file, if a top-level absolute import's first name (`app` in
   `from app import ...`) is a module in the test file's own directory (or an ancestor up to the
   repo root), and is not importable from the root or `src/`, that directory becomes an import
   root. The roots are recorded in `TestPlan.import_paths` and stated in the report ("added
   `backend/` to the import path because `backend/testapp.py` imports `app` from it"). The driver
   prepends them to `PYTHONPATH` in the test phase (both install methods). An environment
   variable rather than `-o pythonpath=` works with any pytest version a repo pins, and with
   unittest. A repo's own `pythonpath` / `rootdir` config wins over the guess.
3. **unittest parser fixes** (independent of the runner choice): allow anything (log output)
   between `... ` and the status, as long as the status ends a line, and use the summary-backed
   test list from the `ERROR:` / `FAIL:` blocks to fill in tests the line regex missed. The
   block regex allows the optional docstring line before `----`.
4. Correct the misleading sandbox note: "no setup.py/pyproject.toml; repo on PYTHONPATH" vs
   "offline editable install failed; ...".

Caveat to state in CLAUDE.md: with `backend/__init__.py` present, pytest imports the test as
`backend.testapp` while the test imports the code as `app`. That's fine here, but a repo that
imports the same file under both names gets two module copies (a mock on one doesn't affect the
other). apifix won't try to detect that.

### D. Module roots for whole-file checks (fixes 4.5)

`code_checks.MODULE_ROOTS` becomes `("", "src/", *test_plan.import_paths)` per job, so
`app.client` resolves to `backend/app.py` for mock-target checks, code-under-test checks and
suggestions.

### E. openai-v1 guidance for this repo's two runtime traps (fixes 4.3, 4.4)

- **Key**: when the job has no `OPENAI_API_KEY` placeholder (no CI sets it), the job guide gets
  one extra line: "The tests run without OPENAI_API_KEY: do not create the client at import
  time. Create it where it's used (or in a small cached helper), so tests can patch it."
  The alternative, applying the placeholder whenever the migration declares it, would change a
  hard rule. I don't recommend it, but it is your call (question 1).
- **Dependencies**: the guide's dependencies part says that when the file pins exact versions,
  bump the pins openai 1.x needs too (`typing_extensions>=4.11`, or keep it and pin
  `httpx<0.28`). A new `failure_hints` marker, `unexpected keyword argument 'proxies'`, adds
  "openai < 1.55.3 is incompatible with httpx 0.28: pin openai>=1.55.3 (and
  typing_extensions>=4.11), or httpx<0.28". The verifier feedback to the dependency file
  includes it, since the error shows up in test output, not in pip's.

### Not changing

- The missing `resumes/` PDFs: nothing apifix can or should do. The 4 tests are
  `no_signal_preexisting` on both sides, and the report already lists them. The live run's
  report should be read with that in mind.

## Expected result on this repo after A-E

Baseline: 8 tests, 4 passed (home x2, resume-feedback x2), 4 failing (missing PDFs). Findings:
3 in `app.py`, the pin, and 4 in `testapp.py` (2 patch strings, 2 dict mocks). Whole-file sends
`requirements.txt`, `app.py`, `testapp.py` (with `app.py` as context). A verified fix needs:
lazy client creation, attribute access, consistent pins, and a patch target on the client
`app` uses with object-shaped mock responses. Tampering checks cover `testapp.py`.

## Tests (synthetic fixtures only; nothing copied from the repo)

New fixture `tests/fixtures/nested_flat_imports/` (made-up names, a few lines each):
root `requirements.txt` (exact pins incl. `openai==0.28.1`), `svc/__init__.py`,
`svc/server.py` (a function calling `openai.Completion.create`, `logging.info` on stderr in the
success path), `svc/testserver.py` (`unittest.TestCase` with docstrings; `from server import ...`;
`@patch('server.openai.Completion.create')` returning a `{"choices": [{"text": ...}]}` dict;
one test reading a gitignored data file).

- Scanner: prefix strings (`a.openai.X.create`, `a.b.c.openai.error.RateLimitError`), the local
  `openai` module guard, prose strings untouched, dict rule in `testserver.py`.
- `is_test_file`: name cases (`testapp.py`, `tests.py`, `test_x.py`, `x_test.py`, `contest.py`
  no), content cases, a configured `python_files`, and the verifier using the original's source.
- Verifier: weakening an assert in `svc/testserver.py` is rejected as tampering.
- `detect_tests`: explicit file list, `import_paths == ("svc",)`, repo config overriding the
  guess, no import root when the name is importable from the root.
- unittest parser: interleaved log line, docstring line in `ERROR:` blocks (from recorded text).
- code_checks: `server.client` resolves through `import_paths`.
- Docker: the fixture's baseline runs with N passed and the data-file test failing, not
  "no test files found".
- openai-v1: the job-guide key line appears only without a placeholder; the `proxies` hint.

Then a live whole-file run on the real repo (not committed; results recorded like the last
one).

## Questions for review

1. API key: keep the CI-only placeholder rule and steer the model to lazy client creation (as
   proposed), or apply the placeholder for every openai-v1 job?
2. Runner: pytest with an explicit file list for everything (proposed), or keep unittest when
   all test files are unittest-only, as today?
3. Test-file names: is `test*.py` (unittest's default, so also `testing_utils.py`) broad enough,
   or too broad?

## Amendments (approval, verbatim)

Approved, with these answers and one addition. Record them verbatim in the plan’s Amendments section, then build it.

API key: keep the CI-only rule. Update the openai-v1 guide to prefer creating the client where it’s used (inside the function, or lazily) instead of at import time.
Test runner: run all tests through pytest with an explicit file list, using the import-path approach you recommended. Keep unittest only as a fallback if needed; if it stays, fix both parsing bugs (log lines between ... and ok, and empty messages for tests with docstrings).
Test files: test*.py, *_test.py, files under tests/ directories and conftest.py, plus content detection (imports unittest or pytest and defines Test* classes or test_* functions). Use the same definition everywhere, including the verifier’s tampering checks.
Addition, a post-install smoke check: each migration can define a small check that runs in the sandbox after installing dependencies. For openai-v1: import openai and create a client with a dummy key. If it fails, report it to the model as a dependency problem, including the installed versions of the migrated library and any pinned packages that limited it (e.g. typing_extensions==4.5.0 forcing openai 1.5.0).

Also include the scanner fix and the mock-target check’s module lookup (the tests’ own directory and the import path chosen). Add tests using synthetic reproductions only. Run the full suite with --docker and ruff, then commit. Don’t run the live tests.

## Build notes (2026-10-15)

Where the build follows the amendments in a way worth stating:

- Test files: the amendment's list, plus two things kept or added in the safe (inclusive)
  direction: `test/` and `testing/` dirs still count (they did before), and a class counts by
  content when it subclasses `*TestCase` even if its name does not start with `Test`
  (`class AppTestCase(unittest.TestCase)`). The verifier decides by the original file's
  content, so an edit cannot make a test file stop counting.
- Runner: pytest with an explicit file list for every repo. The explicit list holds files that
  match the names or define tests; `conftest.py` and helpers that only sit under `tests/` are
  test files (tampering checks) but are not given to pytest to collect. A repo whose pytest
  config sets `testpaths` or `python_files` keeps its own collection (an explicit list would
  bypass it); its `pythonpath` replaces the import-path guess. unittest is not chosen by
  detection any more; the runner and the fixed parser remain for an explicitly passed plan
  (`Sandbox.session(tests=...)`), so no fallback logic was added.
- Smoke check: runs for the fixed code only (the baseline has 0.x installed). "Pinned packages
  that limited it" are reported as the repo's exact pins on the library or any of its
  requirements (pins that can hold it back); apifix does not analyse pip's resolution.
- Kept from the plan: the `proxies` failure hint and the dependencies guide note, alongside the
  smoke check.
- Existing tests changed because the approved behaviour changed: detection tests now expect
  pytest with explicit files (the unittest-discovery expectations, including the `-s tests`
  start-dir case, were replaced); the unittest sandbox and Docker tests pass an explicit
  unittest plan; `is_test_file` tests moved from test_tampering.py to test_project.py with
  every earlier case kept.
