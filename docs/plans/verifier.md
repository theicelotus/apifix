# Plan: engine/verifier.py

Approved 2026-10-06. Recovered from the session transcript; the request and amendments are verbatim.

## Request

Next: engine/verifier.py. It compares a baseline sandbox run (original code)
with a run on the fixed code and produces a verdict. Requirements:

Checks:
- Per-test comparison by test ID: newly failing, newly erroring, newly
  skipped, and deleted tests (present in baseline, missing after). Tests that
  already failed in the baseline are not blamed on the fix.
- Flakiness: rerun any newly failing test once (or the test phase, if
  simpler); if it passes on rerun, mark it flaky rather than a regression.
- Test tampering: inspect the diff for edits to test files. Allowed: updating
  mock/patch target strings and imports to the new API (the scanner already
  flags these). Flagged: removed tests, removed/weakened asserts, added
  skip/xfail markers, broadened except blocks in tests.
- Migration completeness: rerun the scanner on the fixed code; any remaining
  "certain" findings mean the migration is incomplete.
- Evidence strength: measure line coverage of the lines the fix changed
  (coverage installed in the sandbox for this run). Report what share of
  changed lines were executed by tests.

Verdict: verified | verified_weak (passed, but low coverage of changed lines)
| rejected (regressions, tampering, or incomplete) | unverified (no usable
tests), with machine-readable reasons the agent can use when retrying, and a
human-readable report section for the eventual pull request.

Note: tests that call the real OpenAI API will fail in both runs (no network)
and give no signal; report them as such rather than as passes.

Reuse test_results.py and the sandbox's per-test IDs. Unit tests with
recorded/fake sandbox results; one Docker integration test with a tiny repo
covering a good fix, a fix that breaks a test, and a fix that deletes a test.
Explain the plan before writing code.

## Plan

### Shape
```python
verify(original: Path, fixed: Path, migration: str, *,
       sandbox: Sandbox, baseline: SandboxResult | None = None,
       config: VerifierConfig = VerifierConfig()) -> Verification
```
`baseline` can be passed back in. The original code doesn't change between agent retries, so its sandbox run happens once per job rather than once per attempt (up to 4 runs).

**New modules** (each pure and tested on its own; the verifier only combines them):
- **`engine/diffing.py`:** compares the original and fixed trees file by file and returns the changed files plus the changed line numbers in the fixed version.
- **`engine/tampering.py`:** compares old and new test files by AST (parsed Python syntax trees, not raw text).

### Changes to existing code
1. **Sandbox sessions:** a `SandboxSession` installs dependencies once and can then run the test phase several times. Each run starts from a fresh copy of `/work`, so leftovers from one run can't affect the next. `run_sandbox()` keeps its current behavior on top of this. The flaky-test rerun only re-runs the test phase, not the install.
2. **Coverage option:** when enabled, the session adds `coverage` to the install requirements. The driver then runs the tests under `coverage run --source=/work` (data file in `/results` via `COVERAGE_FILE`) and writes `coverage.json`. This only applies to the fixed run.
3. **`TestCaseResult.message`:** each result gets the first ~500 characters of its failure/error text, from JUnit, or from the `ERROR:`/`FAIL:` blocks in unittest output. This is needed to tell network/credential failures apart from real ones.

### Checks
**1. Per-test comparison by ID (baseline vs fixed)**
- **Newly failing / newly erroring:** passed in the baseline, fails or errors now.
- **Collection errors:** if a test module fails to import in the fixed run, pytest reports one error for the whole file and its individual tests vanish. Those tests are reported as `collection_error`, not as deleted.
- **Deleted:** in the baseline, missing now, and not explained by a collection error.
- **Not blamed:** tests that failed or errored in the baseline, whatever happens to them now.
- **No signal:** tests that fail in both runs. If the failure message shows a network or credentials problem (`APIConnectionError`, name-resolution errors, a missing API key, …), they're labeled `needs_external_service`; otherwise `preexisting_failure`. They're reported but never counted as passes.
- **Fixed by the change:** failed in the baseline, passes now. This is good evidence: when the old code breaks against the new library, the baseline fails and the fix makes it pass.

**2. Flakiness:** if anything newly fails or errors, the test phase reruns once in the same session. Tests that pass on the rerun are marked `flaky`, not counted as regressions, and listed in the report.

**3. Tampering** (AST comparison of test files: `test_*.py`, `*_test.py`, `tests.py`, `conftest.py`, and anything under `tests/` or `test/`)

| Check | Severity |
|---|---|
| Test function removed (a rename with an identical body is allowed) | reject |
| Fewer assertions in a test. Assertions counted: `assert`, `self.assert*`, `mock.assert_*`, `pytest.raises` / `assertRaises` blocks | reject |
| Assertion weakened: made trivially true, or downgraded from equality/identity to a membership/comparison check, or down to a truthiness or `is not None` check. Matched by position within the test | reject |
| Skip/xfail added: decorators, `pytest.skip`/`xfail`/`importorskip`, `self.skipTest`, `pytestmark` | reject |
| `except` or `pytest.raises` broadened to bare / `Exception` / `BaseException`, or a new try/except wrapping test code | reject |
| pytest config changed (`addopts`, `testpaths`, `--deselect`/`--ignore`, `collect_ignore`, collection hooks in conftest) | reject |
| A literal expected value in an assertion changed | warn (shown in report, doesn't reject) |

Allowed without flags: patch-target strings, imports, mock return values and other setup code, and response-access rewrites inside asserts (`r["choices"][0]` → `r.choices[0]`). To make that last one work, string keys used as subscripts are normalized away before comparing assertions.

**4. Completeness:** the scanner runs on the fixed tree. Any remaining `certain` finding, including a dependency still pinned below 1.0, means `incomplete_migration`. Remaining `likely` findings are listed for review but don't reject. The report also says how many of the original findings were resolved.

**5. Evidence strength:** for changed non-test Python lines, executed ÷ executable, using coverage's own definition of an executable line. Files the tests never imported count as 0% (that's what `--source` gives). Changes to requirements and other non-Python files are listed but not counted. If coverage couldn't be measured, the result is `coverage_unavailable`.

### Verdict, in order
1. **`rejected`:**
   - newly failing, newly erroring or deleted tests (excluding flaky ones)
   - collection errors
   - any reject-level tampering
   - an incomplete migration
   - the fixed run errors or times out while the baseline didn't
2. **`unverified`:** no usable evidence. No tests at all, no fixed-run test passes, everything is "no signal", or both runs hit the same sandbox error.
3. **`verified_weak`:** passed, but changed-line coverage is below `min_changed_line_coverage` (default 0.5), coverage was unavailable, or tests became skipped without the diff adding a skip.
4. **`verified`:** everything else.

**Output:**
- **For the agent:** `reasons: list[Reason]`. Each has a stable `code` (`regression`, `deleted_test`, `tampering.assert_removed`, `incomplete_migration`, `low_changed_line_coverage`, …), the affected test IDs or file:line, and a one-line hint for the retry.
- **For the pull request:** `report_markdown()` writes the PR section: verdict, a baseline-vs-fix table of counts, changed-line coverage, tests fixed by the change, no-signal and flaky tests, tampering warnings, and remaining findings.

### Tests
- **Unit:** recorded `SandboxResult`s and a fake session, so no Docker:
  - each per-test category, including collection error vs deleted
  - flaky rerun
  - each tampering kind, plus the allowed edits (patch-target rewrite, `["choices"]` → `.choices`) producing no flags
  - completeness
  - coverage maths
  - verdict precedence
  - Markdown snapshot
- **One Docker integration module:** a tiny repo pinned to `openai==0.28.1` whose tests mock `openai.ChatCompletion.create`. One shared baseline, then three fixes:
  - **good fix** (new client, updated patch target, `openai>=1.0`) → `verified`
  - **fix with a bug** → `rejected` (regression)
  - **correct fix that deletes a test** → `rejected` (`deleted_test` + `tampering.test_removed`)

  This is 4 installs, a few minutes in total.

### One decision for you
**Newly skipped tests:** I'd reject only when the diff added the skip, which is tampering. A test that becomes skipped through a pre-existing `skipif` (e.g. one keyed on the openai version) is lost evidence, not cheating, and the agent couldn't fix it by retrying. So that case would lead to `verified_weak`. If you'd rather reject every newly skipped test, say so.

If this looks right, tell me to go ahead (with your choice on skipped tests) and I'll build it.

## Amendments (approval)

Approved. On skipped tests: agree. Reject only when the diff adds a skip;
a pre-existing skipif that now triggers -> verified_weak. Amendments:
1. Editing the condition of an existing skipif/xfail so that it now skips
   counts as adding a skip (reject).
2. Edits to mock return values / test setup data stay allowed, but list
   them in the report as "info" (file:line) so reviewers can check them.
3. Scope check: warn (don't reject) on changed files that had no scanner
   findings, and on any changes to CI/tooling config (.github/, tox.ini,
   noxfile, Makefile, setup.cfg/pyproject sections other than dependency
   pins). Include them in reasons and the report.
Then build it.

## Later changes

Finished after a power loss (commit 4ccce4d). Additions beyond the plan:
- A failed baseline run with a passing fix is `verified_weak` (`no_baseline`).
- A changed skipif/xfail condition that skips nothing is listed as info.
- `apifix cleanup` removes containers/temp dirs left by interrupted runs; Docker tests compare containers before/after instead of requiring none.
