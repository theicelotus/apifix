# Fixes from the the hosted provider run on resume-analyzer

## Request

Context: Read CLAUDE.md. A the hosted provider run on the resume-analyzer (~/apifix-runs/resume-gptoss-1/) shows four problems:

(a) The baseline install exceeded 900 s on slow PyPI downloads, so there was no baseline.
(b) Attempt 1’s fix looks correct, but the verifier rejected it with regression (“tests that passed before the fix now fail”) even though no_baseline was also reported. The failing tests are probably the four that need gitignored PDFs, which fail on every version.
(c) The model took 84 s in total, the sandbox 1,892 s.
(d) The job aborted with HTTP 413: the hosted provider’s free tier allows 8,000 tokens per minute for this model and counts the reply reservation, so prompt ~3,265 + --max-output-tokens 6000 = 9,265 was refused.

Task: record this request verbatim in docs/plans/hosted-run-fixes.md, write the plan, then build it.

Verifier with no baseline: find out why regression was reported without a baseline, and fix it. Without a baseline result, failing tests in the fixed code must never count as regressions. The verdict should be unverified, with a clear reason.
Persistent pip cache shared across sandbox runs, so packages downloaded once are reused (the test phase stays network-less). Report download size or cache hits if easy.
Install retries: treat “from versions: none” as a possible network failure and retry it like other transient failures.
apifix verify <repo> --migration openai-v1 --diff <file> --out DIR: apply a saved diff to a copy and run the same verification as fix, with no LLM call, writing the same report files.
Reply budget per request: in whole-file mode, size max_tokens from the file being rewritten (its estimated token count plus a margin, capped by --max-output-tokens). When a 413 or 429 message states a per-minute limit (“Limit 8000, Requested 9265”), retry that request once with max_tokens lowered to fit, if the result still leaves room for the file.
Hosted endpoints: the “model not found” error shouldn’t list local hardware suggestions.

Add tests (synthetic fixtures only), run the fast suite and ruff, then commit. Run the Docker suite only if the network is fast enough; otherwise say so in the commit message.

## Findings

- (b) `verifier.compare_runs` treated a missing baseline as "the original had no tests": every
  test of the fixed run became a new test, and new tests that fail go into `newly_failing`,
  which `_delta_reasons` reports as `regression` ("tests that passed before the fix now
  fail"). A baseline whose test phase started but produced no results (status `error`: pytest
  crashed without a report, OOM kill, unreadable report) had the same effect. The four tests
  open `resumes/*.pdf`, which the repo does not contain, so they fail on every version.
- (c) Install time, not the test phase: the fixed run's install alone took 799 s, the
  baseline's two attempts 186 s + 900 s. Every run downloads everything again
  (`PIP_NO_CACHE_DIR=1`, a fresh venv per run).
- (d) The request for `backend/testapp.py` (5.5 KB) reserved 6,000 reply tokens; the server
  counts prompt + `max_tokens` against its 8,000 tokens per minute.

## Plan

1. **Verifier without a baseline.** A baseline is usable when its test phase ran and its
   status is not `error`/`timeout`. Without one there is no per-test comparison: failing or
   erroring tests of the fixed run (collection errors included) go into a new
   `TestDelta.failing_without_baseline`, never into `newly_failing` / `newly_erroring` /
   `broken_modules`, and there is no flaky rerun (nothing to confirm). `new_tests` stays
   empty (every test would be "new").
   - Reasons: `no_baseline` stays `weak` (so a fix whose tests all pass is still
     `verified_weak`, as now). New `unjudged_failures`, severity `unverified`: "N test(s) fail
     on the fixed code, but the original code's tests did not run (<reason>), so apifix
     cannot tell whether the fix broke them; they are not counted as regressions." Verdict
     `unverified`, unless an independent reason rejects (incomplete migration, tampering,
     syntax error, failed smoke check, failed fixed run).
   - Report: a "Failing tests (no baseline to compare with)" section. The verdict titles for
     `unverified` say "not enough evidence" instead of "no usable tests", which was wrong for
     this case.
   - Agent: an `unverified` verdict with `unjudged_failures` is not retried (no edit can
     produce the missing baseline); the report suggests re-checking the diff with
     `apifix verify` once the original's install works.
2. **Persistent pip cache** (install phase only).
   - `SandboxConfig.pip_cache_dir` (None = off, the library default, so unit tests never
     touch the home dir). The CLI turns it on: `$APIFIX_PIP_CACHE`, else
     `$XDG_CACHE_HOME/apifix/pip`, else `~/.cache/apifix/pip`; `--no-pip-cache` turns it
     off for `fix` and `verify`.
   - Mounted read-write at `/pipcache` in install containers only, with
     `PIP_CACHE_DIR=/pipcache` and without `PIP_NO_CACHE_DIR` (pip treats any value of it as
     "no cache"). Test and smoke containers never see it; the test phase stays
     `--network none`.
   - Only a dir apifix created is mounted (`:z` relabels it for SELinux): apifix creates it
     with a marker file `.apifix-pip-cache`; an existing non-empty dir without the marker is
     not used (cache off for that run, with a note).
   - Download stats: the driver passes pip's output through and counts `Downloading <file>
     (<size>)` vs `Using cached <file> (<size>)` (distribution files, not `.metadata`), then
     prints one marker line `APIFIX_INSTALL_STATS {...}` (the output tail is kept, so the
     line survives truncation). `PhaseResult.downloads` holds it; trajectories keep it per
     install attempt; the report gets one line per run ("baseline: 31 files, 24.1 MB
     downloaded, 0 from the cache").
   - Trust: install containers run third-party build scripts, so a malicious package could
     plant files in the shared cache that later runs (other repos too) install. The repo's
     own code still never runs with the cache or the network. Documented, with
     `--no-pip-cache` as the opt-out.
3. **Install retries:** `(from versions: none)` (what pip prints when the index was
   unreachable) is a network marker, so the install is retried once even though "Could not
   find a version that satisfies" is a requirement marker.
4. **`apifix verify <repo> --migration M --diff FILE [--out DIR] [--no-pip-cache]`**
   (`engine/verify_diff.py`). Copies the repo (`copy_repo`), applies the diff with
   `git apply` in the copy (git never looks above the copy; `git apply` refuses paths outside
   it), rejects diffs that create symlinks, then runs the same baseline + `verify()` as `fix`
   (same env placeholders), with no LLM. Writes `fix.diff` (the applied changes),
   `report.md`, `verification.json`, `trajectory.json` (job, findings, baseline, the
   verification, timing). Exit 0 verified / verified_weak, 1 rejected / unverified, 2 usage
   errors (diff does not apply, ...).
5. **Reply budget.**
   - Whole-file mode: `max_tokens = min(--max-output-tokens, 2 * est + 2,048)`, `est` =
     bytes / 3 of the larger of the original file and its current version. The margin covers
     a rewrite that grows and reasoning models that think before answering (the run's 4.2 KB
     `app.py` took 3,298 reply tokens). Recorded per response (`max_tokens`); the pre-call
     budget check uses it.
   - 413, or 429, whose message states a per-minute limit ("tokens per minute (TPM): Limit
     8000, Requested 9265"): the provider raises `RequestTooLarge` (an `LLMError` with
     `limit` and `requested`); for 429 only when `requested > limit` (otherwise waiting helps,
     as now). The agent retries that request once with `max_tokens = limit - (requested -
     max_tokens) - 64`, if that is at least the file's `est` (agent mode: 1,024), and notes
     it; otherwise the job stops with the server's message and why lowering cannot help. For
     the run: `testapp.py` est 1,843, budget 5,734, prompt 3,265 -> 413 -> retried with 4,671.
6. **Hosted endpoints:** "model not found" lists the server's models (when it lists them)
   and no hardware suggestions; the same for the no-tool-support errors, where hardware is
   irrelevant too. Local endpoints keep `MODEL_SUGGESTIONS`.
7. Tests (synthetic fixtures), README and CLAUDE.md, fast suite + ruff, Docker suite if the
   network allows, commit.

## Approval

The request asked for the plan and the build in one go; no amendments.

## Found while verifying on the real repo: CRLF files

`apifix verify` on the run's attempt-1 diff failed with "patch does not apply": the repo's
`backend/*.py` use CRLF, and `diffing.read_text` read files with Python's newline translation,
so `fix.diff` had LF lines (not `git apply`-able, though documented as such). The same read
fed the agent's snapshot/restore of the best attempt, which would have rewritten a CRLF file
with LF endings. `read_text` now keeps line endings (`newline=""`), the restore writes them
back unchanged, `apifix verify` reads the diff and the CLI writes `fix.diff` without
translation. A CRLF -> LF conversion now shows as changed lines, which it is.

## Status

- Fast suite and ruff pass. Full `pytest -q --docker`: the first run (cold pip cache) had one
  install timeout (`test_smoke_check_reports_versions_and_limiting_pins`, 300 s, slow PyPI);
  it passed when rerun, and a second full run passed (295 s instead of 554 s with the cache
  warm).
- Real repo: `apifix verify ~/apifix-targets/resume-analyzer --migration openai-v1 --diff
  <attempt 1, CRLF-correct>`: **verified** in 93 s (baseline 64 s, verification 29 s; the
  the hosted provider run's sandbox time was 1,892 s). Baseline 4 passed / 4 failed, fix 4 / 4: the four
  tests that open the missing `resumes/*.pdf` fail on both and give no signal. Downloads:
  baseline 27 files (16.2 MB) plus 7 from the cache; the fix's install 0 downloaded, 45 from
  the cache.
