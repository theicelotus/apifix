# Plan: follow-ups from the first google-genai benchmark runs

Approved 2026-10-31 (no amendments). Evidence: ~/apifix-runs/*-genai-1 and *-genai-verify-1
(verify = the same diffs, re-checked with a working baseline).

## Request (verbatim)

0. Retry tuning (from 5040dbc): only failures that clearly look like
   network errors (ReadTimeoutError, connection reset/refused, DNS) get
   up to 3 retries. A failure of unknown cause gets at most 1 retry, as
   before. Make the note say which kind it was.

google-genai migration:
4. String patch targets: tests patch "ailabel.lib.llms.genai.GenerativeModel"
   (a name the new SDK doesn't have). Scan string targets in patch()/
   mock calls that name old-SDK attributes (genai.GenerativeModel,
   genai.configure, google.generativeai.*) as findings in test files,
   with the new target in the suggestion.
5. google.api_core: agentic-tictactoe's source AND tests import
   google.api_core.exceptions (tests/unit/llm/test_gemini_provider.py:10);
   google-genai no longer installs google-api-core, so 4 test modules
   stopped importing. Make google.api_core usage a finding in any file
   (tests included), migrated to google.genai.errors per amendment 3. If
   any file still imports google.api_core after the fix and
   google-api-core is not declared, the completeness check must reject
   it (or the pin edits add google-api-core; say which you chose).
6. Changed contracts: ailabel's fix changed what get_gemini() returns, and
   tests that patch get_gemini (with no findings of their own) were never
   sent to the model. In whole-file mode, also send test files that patch
   a function defined in a file being rewritten, so they can be updated
   to match. Add to the guide: keep the repo's own function signatures
   and return shapes where possible.

Add fixtures shaped like these cases and tests for each. Write the plan
doc as usual. Run the full suite once, then ruff. Commit each group
without Co-Authored-By lines and push to origin main.

## Plan

Three commits: (0) retry tuning, (4+5) google-genai scanner findings, (6) contract tests.

**0. Retry tuning (`sandbox.py`).** `NETWORK_ERROR_MARKERS` splits into
`CLEAR_NETWORK_MARKERS` (timeouts, connection reset/refused, remote disconnect, incomplete read,
DNS, unreachable) and `AMBIGUOUS_NETWORK_MARKERS` (`Max retries exceeded`, `SSLError`, HTTP
429/5xx, `(from versions: none)`: these can be a bad pin or a proxy as well as the network). A
clear marker allows `MAX_NETWORK_INSTALL_RETRIES` (3) retries; an ambiguous marker or no
recognised marker allows `MAX_UNKNOWN_INSTALL_RETRIES` (1). The note says
`dependency install failed (network error); retried N time(s)` or `(unknown cause)`.

**4. String patch targets (`google_genai/python_checks.py`).** In a test file, the first string
argument of `patch(...)` / `monkeypatch.setattr(...)` and the `(module, "name")` form of
`patch.object` are checked: a dotted target that goes through `genai.<name>` with `<name>` a
removed module-level name (`GenerativeModel`, `configure`, `embed_content`, ...) or that starts
with `google.generativeai` is a finding on the string, whether or not the test imports the old
SDK. Suggestion: patch `<prefix>.genai.Client` instead (and set `.models` / `.chats` return
values), with the rule's own text for what replaced the name.

**5. google.api_core (`python_checks.py`, `repo_findings.py`, `rules.py`).** Usage of
`google.api_core` in any file is a finding: every import of it (category `import`), and every use
of an exception under `google.api_core.exceptions` (attribute or imported bare name; category
`error_class`), no longer gated on the file importing the old SDK. Names with no table entry map
to `APIError` ("pick ClientError for 4xx, ServerError for 5xx"). A repo-level pass
(`adjust_findings`, which has the repo root) then decides, once, for all of them:
- no old/new SDK finding anywhere and no `google-generativeai` / `google-genai` declared: it is
  not a Gemini repo, drop them (a file that only uses api_core for another Google client);
- `google-api-core` (or a distribution that depends on it: `google-cloud-*`,
  `google-api-python-client`) not declared: `certain`. The migrated code has no
  `google.api_core` to import, so the completeness check (verifier.incomplete_findings, run
  on every whole-file rewrite) rejects a file that still imports it;
- declared: error findings stay `likely` ("keep it if the file uses it for something else
  such as retries"), the import findings are dropped.

**Choice: reject, not add the pin.** Adding `google-api-core` to the pins would make the sandbox
keep working for code that has no use for it and would leave a dependency the migration does not
need; the old SDK's transitive dependency is not something the repo declared. A file that
really needs api_core for retries should declare it, and that is a human decision.

**6. Contract tests (`contracts.py`, `agent.py`, `whole_file.py`).** `contract_tests(repo,
rewritten)` finds the test files, not already sent, that patch a name defined at the top level of
a file being rewritten: a `patch` / `monkeypatch.setattr` / `patch.object` target whose module
part is that file's module, or a repo module that imports the name from it (absolute or
relative). In whole-file mode these test files are added to the files sent (last, as tests are),
with a "Tests that patch code this migration rewrites" section in their prompt naming each patch
and the rewritten function, and `required=False` (they may need no change). They get no scanner
findings, so the unrelated-edit filter (which would revert every hunk of a file with none) is
off for them; tampering, weakened-assertion and suspicious-edit checks still run in the verifier.
The guide gets a rule: keep the repo's own function signatures and return shapes where possible;
when one must change, say so in a comment and update the callers and tests that patch it.

## Built

- **0** `sandbox.py`: `CLEAR_NETWORK_MARKERS` / `AMBIGUOUS_NETWORK_MARKERS`,
  `MAX_UNKNOWN_INSTALL_RETRIES = 1`; the note names the kind. Tests in `test_sandbox.py`
  (clear errors retried 3 times, unknown/ambiguous once) and `test_verifier.py`.
- **4+5** `google_genai/python_checks.py`: string patch targets (`patch`, `monkeypatch.setattr`,
  `patch.object(module, "name")`) in test files; `google.api_core` imports and exception uses in
  any file. `rules.py`: more `API_CORE_ERRORS`, `API_CORE_OTHER`, `API_CORE_MARK`,
  `PATCH_TARGET`. `repo_findings._api_core`: drops them in a repo with no Gemini signal,
  makes them `certain` when google-api-core is not declared (the completeness check then
  rejects a rewrite that keeps the import), keeps only the exception findings, `likely`, when
  it is declared. Chose rejecting over adding a pin (see the plan above). Guide: errors and
  tests parts. Fixtures `tests/fixtures/google_genai_repos/{ailabel_like,agentic_like}`; tests
  in `tests/test_google_genai_followups.py`.
- **6** `contracts.py` (`contract_tests`, `ContractPatch`); `whole_file.py` (`send_order`, the
  `contract` section of `file_prompt`); `agent.py` (`_add_contract_tests`, `_own_findings`,
  `wf_contract`, `contract_patches` on the file's trajectory entry and `contract_tests` at the
  top of the trajectory). Guide: "keep the repo's own function signatures and return shapes"
  in the always-sent client part. Tests: `tests/test_contracts.py` (patch forms, where-used and
  relative imports, src layout, prompt section, end to end with the fake LLM).
- Known gaps: see `docs/notes.md` (google-genai migration; whole-file mode).
