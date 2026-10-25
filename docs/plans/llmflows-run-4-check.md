# llmflows: a completeness check before the sandbox (whole-file mode)

## Request

Attempt 6 of ~/apifix-runs/llmflows-gptoss-3 rewrote
llmflows/llms/openai_embeddings.py back to openai.Embedding.create(engine=...).
Every check before the sandbox passed it; only the sandbox caught it
(incomplete_migration), which cost a counted attempt and a sandbox run.

Add a check in whole-file mode, before the sandbox: rescan each rewritten
file with the migration's scanner and apply the same rule the verifier's
completeness check uses (whatever it would call incomplete_migration),
limited to that file. If any such finding remains, it's a check error
that names the line and gives the finding's suggestion, e.g.
"line 77: openai.Embedding.create is still the 0.x API: use
openai.embeddings.create(...)". The check and the verifier must agree:
nothing this check rejects should pass the verifier, and nothing it
accepts should be incomplete_migration there.

Add tests (including a replay of attempt 6's file from trajectory.json:
it must now fail the check). Write docs/plans/llmflows-run-4-check.md
with this request verbatim plus a "Built" section, then commit.

## The verifier's rule

`verify()` scans the whole fixed tree (`scan(fixed, mig)`) and `_scan_reasons` turns every
remaining finding with confidence `certain` into `incomplete_migration` (reject); `likely`
ones are `remaining_likely_finding` (info). Whether a file has a finding, and its confidence,
comes from the per-file pass (`Migration.check_python(rel, tree, source, repo_modules)` or
`check_dependency`); the repo-level pass (`adjust_findings`) only rewrites suggestions. So a
rescan of one file's text, with the repo's module names, gives exactly the verifier's
`incomplete_migration` findings for that file.

## Plan

- `scanner.scan_text(rel, text, migration, modules, others=, root=)`: the findings a scan of
  the repo would report for `rel` if it held `text` (same `_scan_python` /
  `_scan_dependencies` path, text encoded as UTF-8 as whole-file mode writes it), then the
  repo-level pass over these plus `others` (the other files' findings) for the suggestions.
- `verifier.incomplete_findings(findings)`: the shared rule (`certain`), used by
  `_scan_reasons` and the new check, so the two cannot drift.
- `whole_file.check_candidate(remaining=..., old_api=...)`: check `completeness`, run last on
  the completed text (after reverts and added imports, i.e. what is written). One error per
  finding: "line 78: `openai.Embedding.create` is still the openai 0.x API: use
  client.embeddings.create(...) ...". n/a without `remaining`, or when the file does not
  parse / is not a valid dependency file (those checks fail already).
- `Migration.source_name` ("openai 0.x"): how messages name the old API.
- The job passes `remaining` for every whole-file candidate and for the final region batch
  only (earlier batches leave the other regions on the old code). Module names come from the
  working copy; `others` are the job's findings for the other files.
- A file accepted in an earlier attempt that now fails keeps its accepted version (existing
  fall-back), so attempt 6 would have kept attempt 5's `openai_embeddings.py`.

## Built

- `scanner.scan_text(rel, text, migration, modules, others=, root=)`.
- `verifier.incomplete_findings`; `_scan_reasons` uses it for `incomplete_migration`.
- `Migration.source_name` (protocol); openai-v1: "openai 0.x".
- `whole_file.check_candidate(remaining=, old_api=)`: check `completeness`, last, on the
  completed text; one error per finding (`_incomplete_error`: "line N: `<old>` is still the
  <old_api> API: <suggestion>", first letter lowercased for a sentence-case suggestion). n/a
  without `remaining`, on a syntax error, or for an invalid dependency file.
- `_Job._check(..., complete=True)` passes `remaining=self._rescan` (`scan_text` with
  `wf_repo_modules`, the job's findings for the other files, root the working copy); region
  batches pass `complete=final`.
- Fixture `tests/fixtures/llmflows_attempt6` (original file, attempt 6's reply verbatim, the
  attempt 5 version).
- Tests: `test_scanner.py` (scan_text equals a repo scan's findings, suggestions included, for
  every file of five fixture repos; no findings for a file that does not parse);
  `test_whole_file.py` (errors per certain finding, likely ones not; runs on the text as
  written, incl. added imports; n/a cases; on three fixture repos the check's errors are exactly
  the verifier's incomplete_migration reasons per file; attempt 6's reply now fails with four
  errors at 77, 85, 118, 126, the verifier's lines, after the two comment reverts, and passed
  every check without the rescan; attempt 5's version passes); `test_agent.py` (a resend that
  puts the 0.x call back fails `completeness`, falls back to attempt 1's version, and the
  verifier sees no incomplete_migration; the dependency-file test now fails the check and is
  fixed by the check retry instead of a second attempt); `test_region_edits.py` (a first batch
  that leaves a certain finding in a later region gets n/a, the last batch gets the check).
- docs/notes.md: the completeness check under whole-file checks; the run 3 gap now closed.

What the check says on attempt 6's file, with the llmflows repo's findings as context (no LLM,
no sandbox):

- line 77: `openai.Embedding.create` is still the openai 0.x API: use
  openai.embeddings.create(...): the module-level client, which reads openai.api_key (set in
  llmflows/llms/openai.py:56, llmflows/llms/openai_chat.py:63,
  llmflows/llms/openai_embeddings.py:52); the response is a typed object, not a dict
- line 85: `engine=self.model` is still the openai 0.x API: engine= goes through
  call_with_retry(...) to openai.Embedding.create: 1.x methods take no engine=: pass
  model=self.model (on Azure, the deployment name)
- line 118: `openai.Embedding.acreate` is still the openai 0.x API: use await
  client.embeddings.create(...) with client = openai.AsyncOpenAI(api_key=openai.api_key)
  created where it is used (1.x has no module-level async client); the response is a typed
  object, not a dict
- line 126: `engine=self.model` is still the openai 0.x API: engine= goes through
  async_call_with_retry(...) to openai.Embedding.acreate: 1.x methods take no engine=: pass
  model=self.model (on Azure, the deployment name)

In the run, `openai_embeddings.py` had been accepted in attempt 1 (and kept through attempt 5),
so attempt 6 would have kept that version (completeness ok) instead of reaching the sandbox
with the 0.x calls.
