# Unpinned declarations, --sandbox-requirements pins of the migrated library

## Request

Context: Read CLAUDE.md. Run at ~/apifix-runs/suspense-gptoss-1/ on HCAI-Lab-GT/SuspensePerception (cloned at ~/apifix-targets/suspense): pyproject.toml declares "openai" with no version (uv.lock has 1.99.2, while the code uses the 0.x API). I ran with --sandbox-requirements ~/apifix-runs/suspense-sandbox-reqs.txt pinning openai==0.28.1 for the original code. Problems:

(a) The fix sets no pin (the declaration is unpinned), so the fixed run also got openai 0.28.1. The smoke check failed (“no attribute ‘OpenAI’”), all 4 test modules errored on import, the verdict was rejected, and the smoke-check feedback told the model to “raise or drop the pins”, which it couldn’t, so attempts 2–4 were wasted.
(b) The baseline install failed even after a retry.

Task: record this request verbatim in docs/plans/unpinned-and-sandbox-reqs.md, then build it.

Unpinned declarations: when a dependency file declares the migrated library with no version (or a range that still allows 0.x, e.g. openai>=0.27), apifix’s deterministic pin edits set the migration’s pin (openai>=1.0,<2) there, with a scanner finding for it. Lockfiles (uv.lock, poetry.lock, pixi.lock) are only noted (“regenerate it”).
--sandbox-requirements with an old pin of the migrated library: for the fixed run, if no fix edit changes that library, replace its line with the migration’s target spec anyway, and record it in the report (“fixed run: openai==0.28.1 from --sandbox-requirements replaced by openai>=1.0,<2”).
Smoke-check feedback: when the only pin holding the library back comes from --sandbox-requirements, don’t send it to the model and don’t retry; report it as a sandbox-requirements problem.
Baseline install failure: read pip’s error in the trajectory and tell me whether it was the network or a real conflict (e.g. together with openai==0.28.1), and whether apifix’s classification was right.

Synthetic fixtures for tests. Run the fast suite, Docker if the network allows, and ruff, then commit.

## Findings

### (a) Why the fix ran on openai 0.28.1

- `OpenAIV1Migration.check_dependency` flags only declarations whose clauses are *entirely
  below* 1.0. `"openai"` in `[project] dependencies` has no clauses, so the scan had no
  dependency finding, pin edits had nothing to do, and pyproject.toml stayed `"openai"`.
- With `--sandbox-requirements`, the fixed run takes the fix's line only for packages whose
  *detected* requirements differ between the original and the fixed tree. openai's did not
  (`openai` both times), so the user's `openai==0.28.1` stayed in the fixed run.
- The smoke check's "exact pins" list comes from the install plan, which with an override is
  the user's file: `openai==0.28.1` was the user's line, not the repo's, so "Fix the
  dependency file(s): raise or drop the pins that conflict" asked the model for an edit that
  could not change anything. Attempts 2–4 resent the Python files with that feedback.

### (b) The baseline install: network, not a conflict; the classification was right

From `trajectory.json`, `baseline.install_attempts`:

- Attempt 1 (246 s): downloads at ~73 kB/s (`1.8/9.9 MB 73.2 kB/s eta 0:01:50`), then
  `pip._vendor.urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host=
  'files.pythonhosted.org', port=443): Read timed out.`
- Attempt 2 (380 s): `Connection reset by peer` and `Read timed out. (read timeout=15)`
  retries on `pypi.org/simple/openai/` and `/simple/numpy/`, then the same `ReadTimeoutError`
  on files.pythonhosted.org.
- Neither attempt reached the resolver's conflict reporting (`ResolutionImpossible`,
  `Cannot install ... because these package versions have conflicting dependencies`): the
  traceback is in the download step (`prepare_linked_requirements_more` ->
  `response_chunks`).
- Proof there is no conflict: attempt 1's fixed run installed the **same** requirements
  (the user's file, openai==0.28.1 with `together` and the rest; the fix changed no
  dependency) and succeeded on its second install attempt (`status: ok`, 64 files). Its first
  attempt failed with the same `ReadTimeoutError` at ~97 kB/s.
- apifix's classification was right: `ReadTimeoutError` is a network marker, so the install
  was retried once (report: "Dependency install retried once after a failure that looked
  transient: baseline, attempt 1"). The second failure left the baseline unusable for the
  whole job (it is run once and reused), hence `no_baseline` / `unjudged_failures`.
- What the report could say better: `no_baseline` says "dependency install failed", not that
  both tries failed on the network. Recorded under Later.

## Plan

### 1. Unpinned and loose declarations get a finding and a pin edit

- `DeclaredDependency.direct` (new, default False): a direct reference (`name @ url`, Poetry /
  Pipfile / pixi tables with `git`, `path`, `url`, `file`, `hg`, `svn` or `bzr`). These have
  no version to set and are never flagged.
- openai-v1 `check_dependency`: an entry for `openai` that is not direct and does not
  require 1.0 or later (`not entirely_at_or_above(clauses, "1.0")`) is a `dependency`
  finding.
  - Entirely below 1.0: as now (`certain`, "Pin openai>=1.0 ...").
  - Allows both 0.x and 1.x (no version, `*`, `>=0.27`, `<2`, `!=0.28.0`, Poetry `||`
    ranges, which read as unconstrained): `likely`, with the suggestion "openai is declared
    without a version / `>=0.27` still allows 0.x: pin openai>=1.0,<2 (a lockfile or an
    environment can otherwise install 0.x)". `likely` because pip alone picks a new release;
    the finding matters when a lockfile, a constraints file or an existing environment holds
    0.x. A remaining one is `remaining_likely_finding` (info), not a rejection, so a diff that
    leaves an unpinned declaration is not rejected by `apifix verify`.
  - Unconditional: a repo whose code is already on 1.x but declares `openai` unpinned now
    gets this one finding too (scan exit code 1). The declaration does allow 0.x; pinning it
    is cheap and deterministic.
- `pin_edits.py`: an entry with no version spec can be edited. PEP 508 strings get the spec
  inserted right after the name and extras (`openai` -> `openai>=1.0,<2`,
  `openai[datalib] ; marker` -> `openai[datalib]>=1.0,<2 ; marker`); `name = "*"` / `""`
  table values (Poetry, Pipfile, pixi) get their whole string replaced. The re-parse check
  is unchanged (exactly the intended entries, now with `>=1.0,<2`).
- Lockfiles are never edited: the existing note ("not updated: uv.lock; regenerate it") is
  written for `poetry.lock`, `pixi.lock`, `uv.lock`, `pdm.lock` next to an edited
  pyproject.toml (and `Pipfile.lock`, `pixi.lock` next to Pipfile / pixi.toml). The sandbox
  already drops a lockfile pin that contradicts the new declaration (`poetry.lock` with
  openai 0.28.1); `uv.lock` is not read.
- For SuspensePerception: pyproject.toml `"openai"` -> `"openai>=1.0,<2"` by apifix, with
  "not updated: uv.lock; regenerate it". The fixed tree's detected openai requirement then
  differs from the original's, so the existing override logic gives the fixed run
  `openai>=1.0,<2` instead of the user's `openai==0.28.1`. Whole-file mode does not send
  pyproject.toml (its only finding is a pin apifix set).

### 2. --sandbox-requirements: the library's own old pin, for the fixed run

- New `LibraryTarget(package, first_version, spec)` (models.py), built by `verify()` from the
  migration and passed through `Sandbox.session(..., target=)` to `build_install_plan`.
- `_override_plan`, for a fix (`original` given): after the fix's own changed declarations
  are applied, if the fix did not change the library's detected requirement, every line of
  the user's file for the library that is not direct and does not require `first_version` or
  later is replaced by `name[extras]<spec>[; marker]`.
- Recorded as `InstallPlan.upgraded` / `SandboxResult.override_upgraded`
  (`"openai==0.28.1 -> openai>=1.0,<2"`), in the trajectory's run summaries, and in the
  report: the verification's Dependencies line ("Fixed run: `openai==0.28.1` from
  --sandbox-requirements suspense-sandbox-reqs.txt replaced by `openai>=1.0,<2` (no edit of
  the fix changed openai)") and the fix report's top lines.
- The baseline keeps the user's line unchanged (the original code needs 0.x).

### 3. Smoke check failed and only --sandbox-requirements pins hold the library back

- `SmokeResult.pin_sources`: each exact pin -> where it is declared (`InstallPlan.declared`).
- `smoke_reasons`: when the run used an override and every pin in the list is declared only
  by the user's file, the reason is `sandbox_requirements_pin` (unverified) instead of
  `smoke_check_failed` (reject): "the smoke check ... failed: <error>. Installed: ...; The
  only exact pins that can hold openai back come from --sandbox-requirements X: ... This is
  a problem with that file, not with the fix, and no edit can change it: raise or drop those
  pins there and check the diff again (`apifix verify ... --sandbox-requirements FILE`)."
  With no pins, or any pin from the repo's own files, it stays `smoke_check_failed` as now.
- It is an environment reason (`ENVIRONMENT_REASONS`): never sent to the model. Test
  outcomes it explains are not blamed on the fix: `regression`, `regression_error` and
  `collection_error` become `unverified` (message: "... not judged: the sandbox's
  environment held openai back") while it is present. Tampering, deleted tests, remaining
  findings and the other independent reasons still reject.
- Agent: not retried (`_retryable`), like `environment_incomplete`. The fix report says why
  and gives the `apifix verify ... --sandbox-requirements FILE` command.
- With item 2, the user's own line for the library can no longer be the pin; what remains is
  a companion pin in the user's file (e.g. `typing_extensions==4.5.0`).

### Tests (synthetic fixtures)

- `tests/fixtures/unpinned_openai/`: pyproject.toml with `"openai"` in `[project]`, a
  uv.lock, 0.x code and a test. Scanner finding (`likely`), pin edit, lockfile note,
  whole-file mode not sending pyproject.toml.
- Parser `direct`; check_dependency over unpinned / `*` / `>=0.27` / `<2` / `>=1.0` /
  direct references; pin edits of empty specs in every format (requirements with extras and
  markers, `[project]`, Poetry `*`, Poetry table version `*`, Pipfile, pixi, setup.py,
  setup.cfg, environment.yml).
- `_override_plan` with a target: replaced when the fix did not change the library, kept
  when it did, baseline untouched, markers / extras kept.
- `smoke_reasons` with override-only pins, mixed pins, no pins; verdict downgrade; agent not
  retrying; report lines.

## Later

- `no_baseline` / install-failure reasons could name the network markers pip printed and
  that the retry failed too ("both tries timed out reading files.pythonhosted.org"). A
  baseline whose install failed on the network could also be retried at the next
  verification instead of being reused for the whole job.
- Raising a companion pin in the user's --sandbox-requirements file for the fixed run (like
  item 2 does for the library itself) instead of reporting it.

## Built

Items 1-3 as planned (the session that started the build was cut off by a shutdown; the
partial diff was reviewed and kept, with one fix: the verification report passed the whole
run instead of the --sandbox-requirements label to the "Fixed run: ..." text).

- `DeclaredDependency.direct`; openai-v1 `check_dependency` flags declarations that allow 0.x
  (`likely` when they allow 1.x too, `rules.LOOSE_DEPENDENCY`).
- `pin_edits.py`: empty specs get the pin inserted after the name and extras; `*`, `""` and
  unreadable (`||`) table values are replaced whole. Lockfile notes unchanged.
- `LibraryTarget` (models.py) from `verify()` through `Sandbox.session/run(target=)` to
  `build_install_plan`; `_upgrade_library` in the override plan; `InstallPlan.upgraded`,
  `SandboxResult.override_upgraded`, run summary `override_upgraded`, report lines in the
  verification section and the fix report.
- `SmokeResult.pin_sources`; `sandbox_requirements_pin` (unverified, environment reason);
  `library_held_back` downgrades `regression`, `regression_error`, `collection_error`; agent
  `_retryable` stops on it and the report gives the `apifix verify` command.
- Fixture `tests/fixtures/unpinned_openai/` (SuspensePerception's shape: `"openai"` in
  `[project]`, uv.lock, 0.x call, dict mock). Tests in test_dependencies, test_pin_edits,
  test_project, test_sandbox, test_verifier, test_agent.
- README (Dependency pins, --sandbox-requirements) and CLAUDE.md known limitations.

Fast suite 1264 passed; `pytest -q --docker` 1283 passed (no skips); ruff clean. Not rerun
live on SuspensePerception.
