# Plan: mock guidance (code-under-test check, concrete fixes, guide) and smaller prompts

Requested and approved 2026-10-15. The request and amendments are verbatim.

## Request

Context: Read CLAUDE.md, docs/plans/whole-file-mode.md and docs/plans/whole-file-checks.md. A
second live run (trajectory at ~/apifix-runs/wholefile-7b-2.json) was rejected again. The new
checks worked: the model added the SimpleNamespace import and dropped app.client. But following
the message “Patch a name that exists in app.py”, it patched app.ask, the function under test.
The test imports ask before patching, so the real ask ran, and openai.OpenAI() raised because
there was no API key. app.py creates client = openai.OpenAI() inside ask().

Task: record this request verbatim in a new plan file, docs/plans/mock-guidance.md, write the
plan, and stop for my review. Don’t write code yet.

Reject patching the code under test. In the mock-target check, reject a new or changed target
that resolves to a name the test file imports from that repo module (e.g. from app import ask +
patch("app.ask")). Message: patching it replaces the function being tested; patch what it calls
instead.
Name the right fix in the messages. When a target names a missing top-level name (like
app.client) or the code under test, look in the target module for where that name, or the code
it calls, is created. If it’s assigned inside a function from a call on an imported module
(client = openai.OpenAI()), suggest the concrete target and mock chain, e.g.
patch("app.openai.OpenAI") and set OpenAI.return_value.chat.completions.create.return_value.
Fall back to the current message when nothing is found.
Add a testing section to the openai-v1 migration guide with the standard mock patterns for 1.x:
a module-level client (patch("app.client.chat.completions.create")) vs a client created inside a
function (patch("app.openai.OpenAI") with the return_value chain), and a note that
openai.OpenAI() raises without an API key.
Decision for me: a dummy API key in the sandbox. Should the openai-v1 migration set a
placeholder OPENAI_API_KEY in the sandbox environment (network is off, so it can’t be used),
recorded in the report? Give pros and cons.
Smaller prompts for speed. 96% of the last run’s time was the LLM reading ~28k input tokens on
CPU. Propose ways to cut input tokens, for example not sending the guide for dependency files,
or sending only the guide sections that match a file’s findings. Estimate the savings from the
trajectory.

List any other decisions for me at the end.

## Amendments (approval, 2026-10-15)

Approved, with these decisions. Record them verbatim in the plan’s Amendments section, then build
everything except agent mode.

Patch rule scope: per test.
Module calls: yes, also reject import app + patch("app.ask") when that test calls app.ask(...),
per test.
Message style: include a short code example (3–5 lines) in the retry message.
Dummy API key: only when the repo’s GitHub Actions already set OPENAI_API_KEY, recorded in the
report. In every case, give missing-key failures their own explanation in the retry feedback.
Dependency files in later attempts: don’t resend an accepted dependency file unless the install
failed or the verifier reported a dependency problem.
Transient install failures: retry the install once, save pip’s output in the trajectory, and
don’t count the attempt against the model if the retry succeeds.
Later attempts’ starting file: send the original file, plus the model’s previous version as a
short diff with the reason it failed.
Agent mode: not now. Add it to the plan as a later step.

Prompt size: build the per-job guide sections and the other reductions in your “Combined” row.

Add tests for all of this, including a replay of the second live run’s files. Run the full suite
with --docker and ruff, then commit. Don’t run the live tests.

## Later steps

- Agent mode: run the mock-target checks (missing names, code under test, suggestions) on the
  agent's `edit_file` results too.

## What the trajectory shows

Run: qwen2.5-coder-7b on CPU (Ollama), whole-file mode, 3 attempts, 11 LLM requests, 6,811 s
total (LLM 6,553 s, sandbox 216 s). Every attempt produced the same `app.py` (client created
inside `ask()`), and the same `requirements.txt`.

- **Attempt 1:** the test file's first reply had both known bugs. The check retry fixed both, but
  it patched `app.ask`, so the real `ask()` ran and raised `OpenAIError` (no API key).
- **Attempt 2:** the same tree failed at dependency install. See "Other findings" below.
- **Attempt 3:** the model returned its own `app.ask` version unchanged.

### Where the LLM time goes

Each request's input is 2,069 to 3,042 tokens. Duration does not follow input size: request 4
(2,590 tokens) took 206 s, while request 2 (2,208 tokens) took 268 s. Ollama reuses its KV cache
for the prefix a request shares with the previous one, so only the new part of each prompt is
evaluated.

A two-term fit over the 11 requests matches every request within 120 s, and the total within 3%
(6,746 s predicted, 6,553 s actual):

```
duration ≈ 0.213 s × uncached prompt chars  +  0.46 s × output tokens
           (≈ 0.8 s per uncached input token at ~3.8 chars/token)
```

Uncached means not shared with the previous request's prompt. 29.5k of the 108.5k prompt chars
sent were uncached:

| Part | Uncached chars | Est. time | Notes |
|---|---|---|---|
| System prompt (rules 1,169 + guide 6,186) | 7,355 | ~1,570 s (24%) | evaluated **once**, on the first request; cached after that |
| Verifier feedback (problems + failure output) | 6,792 | ~1,450 s (22%) | sent to all 3 files in attempts 2-3, including `requirements.txt` |
| Context files | 4,950 | ~1,060 s (16%) | `requirements.txt` got `app.py` + `tests/test_app.py` (1,007 chars for a 22-byte file) |
| Header + findings | 4,290 | ~920 s (14%) | |
| File contents | 2,899 | ~620 s (9%) | |
| Check-retry turns | 1,945 | ~415 s (6%) | |
| Closing instruction | 1,428 | ~300 s (5%) | |
| Output (967 tokens) | | ~450 s (7%) | |

So the guide is 64% of the tokens sent but only about 20% of the time: Ollama evaluated it once.
Anything that makes the system prompt differ between consecutive requests throws that cache away.

## Plan

### 1. Reject patching the code under test (`code_checks.py`)

**Semantics.** With `from app import ask` at the top of the test, `patch("app.ask")` never
reaches the test's own `ask` binding, so the real function runs: that is what happened here. A
test that calls `app.ask` through the module instead would be testing the mock. Either way the
patch is wrong.

**Rule.**
- A new target (not in the original's targets, as today) equal to `<module>.<name>` is rejected
  when:
  - the candidate has a module-level `from <module> import <name>` (with or without an alias),
  - `<module>` resolves to a repo module through the existing `_module_file`, and
  - `<name>` is a top-level def, class or assignment of that module, not a submodule.
- Only the exact target counts:
  - `patch("app.Bot")` is rejected when the test imports `Bot`;
  - `patch("app.Bot.helper")` is not (patching a method of the class under test is normal).
- Relative imports are not checked, nor `import app` + `app.ask(...)` (see decision 2).

**Scope.** Decision 1 is whether the rule applies per file (as requested) or per test.

**Message:**

```
mock target 'app.ask': tests/test_app.py imports ask from app.py, so patching it replaces
the function being tested; patch what it calls instead[: <suggestion from §2>]
```

**API change.** `mock_target_problems(original, candidate, read_module, *, path="",
library_modules=frozenset())`. `path` is used in the message. `library_modules` is the
migration's `api_modules`, used by §2. `whole_file.check_candidate` passes both. The check stays
whole-file only.

### 2. Name the concrete fix (`code_checks.py`)

A new helper `suggest_patch(module_rel, module_text, name, rest, library_modules) -> str | None`.
`module_rel` is the module's path (e.g. `app.py`); `rest` is what follows the name in the
rejected target (`chat.completions.create` for `app.client.chat.completions.create`).

It finds how the code reaches the migrated library and returns a target plus a mock chain.

**Missing top-level name** (`app.client...`, `client` not top-level):

- Look for an assignment `client = <call>` inside a top-level function or a method of a
  top-level class.
- The call's function must be a dotted name whose root is a top-level import of the module from
  one of `library_modules`.
- Examples:

  | Where the client comes from | Suggestion |
  |---|---|
  | `import openai` … `client = openai.OpenAI()` | `patch("app.openai.OpenAI")` and set `OpenAI.return_value.chat.completions.create.return_value` |
  | `from openai import OpenAI` … `client = OpenAI()` | `patch("app.OpenAI")`, same chain |

- If the target was just `app.client` (empty `rest`), the chain is `OpenAI.return_value (the
  client)`.

**Code under test** (`app.ask`): walk `ask`'s body in source order for calls through the
library, with these roots:

| Root of the call | Example | Suggestion |
|---|---|---|
| a local assigned from a library call | `client = openai.OpenAI()`; `client.chat.completions.create(...)` | as above, with `rest` = the call's path after the local |
| a top-level name assigned from a library call | module-level `client = OpenAI()` | `patch("app.client.chat.completions.create")` and set its `return_value` |
| a top-level function whose `return` is a library call (one level deep) | `get_client().chat.completions.create(...)`, or `client = get_client()` | `patch("app.get_client")` and set `get_client.return_value.chat.completions.create.return_value` |
| a library module directly | `openai.chat.completions.create(...)` | `patch("app.openai.chat.completions.create")` and set its `return_value` |

The first call that resolves wins. Calls that don't go through `library_modules` are never
suggested (`json.dumps`, `question.strip()`), so a wrong suggestion is less likely than no
suggestion.

**Messages with a suggestion.** For a missing name, the current last sentence ("Patch a name
that exists in app.py") is replaced:

```
mock target 'app.client.chat.completions.create': app.py has no top-level name 'client'
(its top-level names: ask, openai); ask() creates it with client = openai.OpenAI(). Patch
the constructor instead: patch("app.openai.OpenAI") and set
OpenAI.return_value.chat.completions.create.return_value
```

**Fallback.** When nothing resolves, both messages stay as they are today (the §1 message
without the suffix).

**Check of the suggestion.** `app.openai.OpenAI` passes the existing check (`openai` is a
top-level import of `app.py`). With it, the live run's tests pass on the model's `app.py`.

### 3. Guide: testing section (`migrations/openai_v1/guide.md`)

The guide already has a `## Tests` section. It shows only the module-level pattern, and the
model followed it into `app.client`. The plan rewrites that section rather than adding a
second one.

The new section:
- opens with the rule: patch what the code under test calls, never the function or class the
  test exercises;
- notes that `openai.OpenAI()` raises `OpenAIError` without an API key, and that tests run
  without one (wording depends on decision 4);
- shows three 1.x patterns, chosen by how the migrated module gets its client:
  1. client created inside the function: `@mock.patch("app.openai.OpenAI")` +
     `OpenAI.return_value.chat.completions.create.return_value = ...` (`app.OpenAI` when the
     module does `from openai import OpenAI`);
  2. module-level client: `@mock.patch("app.client.chat.completions.create")`. It needs a
     module that imports without a key (see decision 4);
  3. lazily built client (`get_client()`, which the Client section recommends):
     `@mock.patch("app.get_client")` + `get_client.return_value.chat.completions.create.return_value`;
- keeps the existing bullets: the class-level `openai.resources.chat.completions.Completions.create`
  alternative, mock data with attribute access and the same values, exception classes;
- adds one bullet: `SimpleNamespace` needs `from types import SimpleNamespace`.

Size: about +700 chars (1,021 → ~1,700). Under the cache this costs ~150 s once per job on this
hardware.

### 4. Dummy API key in the sandbox: pros and cons (decision 4)

Mechanism if yes:
- a new optional migration attribute `sandbox_env` (openai-v1:
  `{"OPENAI_API_KEY": "sk-apifix-sandbox-placeholder"}`, a literal, never read from the host);
- merged into `TEST_PHASE_ENV` for **both** the baseline and the fixed run;
- recorded in the run summary and the report: `- Sandbox env: OPENAI_API_KEY set to a
  placeholder (no network)`.

Pros:
- **Real CI looks like this.** Many repos' CI sets a dummy key, or tests run where the key
  exists. 1.x's most common idiom, a module-level `client = OpenAI()`, then imports in tests,
  and the guide's module-level pattern works.
- **Fewer environment failures.** Missing-key errors are an artifact of the sandbox, not of the
  migration. Without the key they burn attempts on small models that can't reason their way out.
- **Low risk.** Network is off, so the key can't be used. It isn't a secret, and nothing is read
  from the host.

Cons:
- **It hides a real behaviour change.** 0.x read the key lazily, so a module imported fine
  without one. A 1.x module-level `OpenAI()` raises at import. If the user's own test
  environment has no key, the migrated code breaks there while apifix reports green. That is
  the kind of regression apifix exists to catch.
- **It changes the baseline too.** 0.x sets `openai.api_key` from `OPENAI_API_KEY` at import,
  so tests of missing-key handling (`test_raises_without_key`) change outcome in both runs. That
  is consistent but no longer the repo's real behaviour.
- **It would not have saved this run.** With a key, the real `ask()` gets past `OpenAI()` and
  fails on the network call instead (`APIConnectionError`). The bug was the mock target.
- **It is an exception to "nothing about credentials enters the sandbox"**, even if harmless.
  CLAUDE.md's hard rule would need a sentence.

Options:
- **(a) No key (today).** Plus a specific verifier reason when the fixed run fails with the
  `api_key client option must be set` error: "the code builds an OpenAI client without a key
  (at import or in the tested function); mock the constructor or build the client lazily".
  This goes into the retry feedback.
- **(b) Always set the placeholder**, recorded in the report.
- **(c) Mirror the repo's CI.** Set the placeholder only when a GitHub Actions workflow already
  sets `OPENAI_API_KEY` for its test job (`project.py` already reads the workflows), and record
  why. Otherwise do (a).

**Recommendation: (c), with (a)'s verifier reason in both branches.** The sandbox then matches
the environment the repo's tests really run in, and when there is no key the model gets told
what the failure means. (c) is a separate small step. §§1-3 and 5 don't depend on it, except
the guide wording in §3.

### 5. Smaller prompts

Each proposal was simulated on this trajectory: same messages and outputs, with the fit above
applied to the changed prompts (script in the session scratchpad, not committed). "Cached" is
the estimated LLM time on Ollama as observed. "Prompt chars" is what a server without prefix
caching would evaluate.

| | Proposal | Cached (est.) | Prompt chars |
|---|---|---|---|
| S0 | as run | 6,746 s | 108.5k |
| X1 | no guide for dependency files (requested example) | 9,409 s **(+39%)** | 89.9k (−17%) |
| X2 | only the guide sections matching each file's findings (requested example) | 7,675 s **(+14%)** | 54.3k (−50%) |
| G1 | guide sections chosen once **per job** from all findings (system prompt stays fixed) | 6,174 s (−8%) | 79.0k (−27%) |
| D1 | dependency files: no context files, no failure output (problem list kept) | 6,067 s (−10%) | 105.3k (−3%) |
| D2 | later attempts resend a dependency file only if the last run failed at install | 6,069 s (−10%) | 98.0k (−10%) |
| C1 | context files only for test files, and only changed non-test Python files | 5,925 s (−12%) | 104.5k (−4%) |
| F1 | the attempt's verifier feedback goes first in the user message (same for every file, so cached) | 5,780 s (−14%) | 108.5k (0%) |
| F2 | failure output: tests whose first error line repeats an earlier one shown as "(same error)" | 6,433 s (−5%) | 106.5k (−2%) |
| | **G1+D1+D2+C1+F1+F2** | **4,180 s (−38%)** | **66.7k (−38%)** |
| | same, full guide + new Tests text | 4,901 s (−27%) | 100.5k (−7%) |

Notes on the table:
- **X1 and X2 are worse on Ollama.** Both make the system prompt change between consecutive
  requests, so the guide is re-evaluated for every Python file instead of once per job. They
  only win on servers without prefix caching.
- **G1 is the guide trim that helps on both kinds of server.** The guide is split at `##`
  headings. A pure-data table in `openai_v1/rules.py` maps each section, and each Resources row,
  to finding categories and `old_pattern` prefixes:
  - Errors only with `error_class` findings;
  - the "Removed" list only with `removed_module` or removed-API findings;
  - Streaming only with `stream=True`;
  - Async/Azure bullets only when those patterns are found;
  - Tests only when a test file is selected;
  - Dependencies only with `dependency` findings.

  The selection is made once per job and recorded in the trajectory (`job.guide_sections`).
  Agent mode keeps the full guide: the tool loop reads files the scanner didn't flag.
- **F1** also needs the per-file feedback (a check failure from the previous attempt) to stay
  after the shared block, and the shared block to be byte-identical across files. It is.
- **Estimate limits.** One run on one machine, a 3-file repo. On real repos file contents
  dominate and these percentages shrink. A first-attempt success only benefits from G1. Ollama
  caches one prefix per slot (`OLLAMA_NUM_PARALLEL=1`); llama.cpp server and vLLM have similar
  prefix caching, but the size of the win there is unmeasured.

**Recommended scope for this step:** G1, D1, C1, F1, F2. D2 (skip resending dependency files) is
decision 5.

### Tests

`tests/test_code_checks.py`:
- regression cases from this trajectory, verbatim:
  - the attempt-2/3 `tests/test_app.py` (`patch("app.ask")`) against the live `app.py`:
    rejected, with the `app.openai.OpenAI` suggestion;
  - the attempt-1 first reply (`app.client...`): rejected, with the same suggestion and the
    `return_value.chat.completions.create.return_value` chain;
  - a test file patched as suggested: no problems, and it passes on the live `app.py`, run in
    process with a stub `openai` module;
- `from app import ask as do_ask` is rejected; `patch("app.Bot.helper")` is not; a submodule
  import is not; a target unchanged from the original is not;
- per-test scoping (if decision 1 = per test): `test_main` patching `app.ask` while calling
  `main()` passes;
- suggestions:
  - `from openai import OpenAI` → `app.OpenAI`;
  - module-level client;
  - `get_client()` direct and via a local;
  - a direct `openai.x.create` call;
  - a non-library call (no suggestion, fallback message);
  - an empty `library_modules` (no suggestions).

`tests/test_whole_file.py`:
- `check_candidate` passes `path` / `library_modules`;
- G1 section selection per category (pure data, table-driven);
- D1 and C1 context/feedback rules;
- F1 ordering: the shared feedback is a byte-identical prefix across the attempt's files;
- F2 dedupe, with distinct errors kept.

`tests/test_migrations.py`: every guide section has a selector entry, and the full guide equals
all sections joined (so no section can be silently dropped).

`tests/test_agent.py`: replay of the attempt-2 test file in whole-file mode, rejected through
`check_retry` with the suggestion; `job.guide_sections` in the trajectory.

The full suite with `--docker` plus ruff passes before the commit. Live tests are not run unless
you ask.

### Docs

CLAUDE.md:
- whole-file limitations:
  - the code-under-test rule and its limits;
  - suggestions only through the migration's `api_modules`;
  - guide sections per job;
  - context only for test files;
  - feedback first;
- the `rules.py` guide-section table in the layout.

Append "as built" changes to this file.

## Other findings from the trajectory (not in this step unless you say so)

- **Attempt 2's install failure looks transient.** The tree was identical to attempts 1 and 3,
  yet pip failed. The trajectory does not keep pip's output, so the cause is unknown. The
  verifier then reported `deleted_test` (reject) for both tests because none were collected,
  and told the model "Check the dependency changes", which was wrong advice. That attempt was
  lost to infrastructure.
- **Later attempts send the model's previous, wrong test file as "Current contents"**, not the
  original. In attempt 3 the model returned it unchanged. The §1 check now catches that specific
  case, but the anchoring remains.
- **The test file had only [likely] findings** (the string targets
  `"openai.ChatCompletion.create"`), so its prompt said "If the file needs no change, return it
  unchanged".

## Decisions for you

1. **Code-under-test scope.**
   - File level, as requested: any new `patch("app.ask")` in a file that imports `ask`.
   - Per test (recommended): only when the test function the patch applies to (as a decorator,
     or a `with patch(...)` / `mocker.patch(...)` inside it) calls the imported name directly.
     Patches in fixtures, `setUp` or at module level are not checked.

   File level wrongly rejects `test_main` patching `app.ask` while calling `main()`, a normal
   test in a file that also tests `ask`.
2. **`import app` + `app.ask(...)`.** Also reject a new `patch("app.ask")` when the same test
   calls `app.ask` through the module? Recommended yes, under the same per-test scoping.
3. **Suggestion style.** One line, as above (recommended), or also a 3-line code example in the
   check-retry message? The example is clearer for a 7B model and costs ~150 chars per retry.
4. **Dummy API key.** (a), (b) or (c) from §4. Recommended (c) with (a)'s verifier reason, as a
   separate step.
5. **D2: skip resending dependency files** in later attempts unless the last run failed at
   install (−10%). Recommended yes, but it changes "every file is resent with the feedback".
   Going further, apifix could apply the dependency pin itself without the LLM (the scanner
   already knows `openai>=1.0,<2`). That saves about 3 requests per job here, but fixes would no
   longer all be model-proposed.
6. **Transient install failures (separate step).**
   - Retry a failed install once when the dependency files hash the same as the last install
     that worked.
   - Keep the tail of pip's output in the run summary.
   - Don't report `deleted_test` when the fixed run never collected tests.
   - Don't charge the attempt to the model.
7. **Later attempts' starting point (separate step).** Send the original file, with the
   previous version as read-only context plus the verifier's reasons? Or keep sending the
   working copy?
8. **Agent mode.** The §1/§2 checks stay whole-file only for now. Should agent mode's
   `edit_file` run them too (a later step)?

## As built (2026-10-15)

Everything in the amendments except agent mode, which is under "Later steps".

**Mock targets** (`code_checks.py`)
- Code under test is checked per test: the decorated test, a decorated class's `test*`
  methods, or the test whose body holds the `with patch(...)` / `mocker.patch(...)`. It is
  rejected when that test calls the patched name directly (`ask(...)`, an alias, or
  `app.ask(...)` after `import app`).
- Fixtures, `setUp`, helpers and module-level patches are not checked. Neither are methods
  (`app.Bot.helper`) or submodules.
- Message: `mock target 'app.ask' in test_ask: the test calls ask() from app.py directly, so
  patching it replaces the function being tested; patch what it calls instead`, plus
  `: ask() creates client = openai.OpenAI(), so patch("app.openai.OpenAI") and set
  OpenAI.return_value.chat.completions.create.return_value.` and the example when one is found.
- Missing names keep the old head. With a suggestion, "Patch a name that exists in app.py" is
  replaced by `; ask() creates it with client = openai.OpenAI(). Patch the constructor instead:
  ...` (`Patch that instead` for a `get_client()` factory).
- The example is 3 lines in the form the test used (decorator, or `mocker.patch`):
  - its value is `<the test's mock response>`, not `...`, which a model could copy into a test
    that then runs;
  - it is indented, not fenced, because the reply must hold exactly one code block.
- Several errors where one has an example go out as a bullet list (`join_errors`). The report
  shows them on one line.

**Guide**
- `guide.md` has `<!-- part: id -->` markers. `Migration.guide` is the text without them, so
  agent mode keeps the full guide.
- The Tests section was rewritten with three patterns: client inside the function, module-level
  client, and lazily built `get_client()`.
- Per-job selection: `Migration.job_guide(findings)`, with selectors in `rules.py`
  (`GUIDE_SELECTORS`) and Resources rows matched by class name or `client.<path>` in the
  suggestion. Recorded in `job.guide_sections` and in the report.
- For the live run's findings: 4,040 chars, against 7,419 for the full guide.

**Prompt reductions** (the "Combined" row)
- Feedback first. Context only for test files.
- Dependency files get no context and no test output (problems only, plus the last 1,500
  chars of pip's output when the install failed).
- Accepted dependency files are kept unless the install failed or a reason or remaining finding
  points at them (`attempts[].kept_files`).
- Repeated errors are shown once (`feedback_lines(dedupe=True)`; off in agent mode, whose
  feedback is unchanged).

**Later attempts**
- They send the original, plus `previous_version_note`: a diff with one line of context, at
  most 3,000 chars, and the reason.
- After a check failure the rejected candidate is shown as a diff. This replaces the
  whole-file plan's "error only, not the rejected text".

**Missing key**
- `Migration.failure_hints` (openai: the `api_key client option must be set` error) adds
  `Hint: ...` to the whole-file feedback when the marker appears in failing tests' output or
  the test phase output (collection errors included).

**Dummy key**
- `Migration.ci_env_placeholders` (`OPENAI_API_KEY` → `sk-apifix-placeholder-not-a-real-key`)
  is applied when `project.ci_env_vars` finds the name in a GitHub Actions workflow (a YAML key
  or `NAME=` in a step).
- It is passed to the baseline and every verification as `Sandbox.run/session(env=...)` /
  `verify(sandbox_env=...)`, in both modes (it is sandbox behaviour, not an agent-mode check).
- Recorded in `job.sandbox_env_placeholders`, in each run summary (`env_placeholders`) and in
  the report. CLAUDE.md's secrets rule names the exception.

**Install retry** (`sandbox.py`)
- One retry with the same settings when pip's output has a network marker or no recognised
  marker. Requirement errors (`Invalid requirement`, `Could not find a version`, ...) and
  resolution and build errors keep their existing handling. `max_install_attempts` went from 3
  to 4.
- Run summaries keep `install_attempts` (status, exit code, image, duration, and the last 4,000
  chars of output for failed phases), `install_retried` and `notes`. The report says which runs
  retried.
- A successful retry means the attempt is verified on its merits, so it does not count against
  the model.

**Verifier**
- When the fixed run has no test phase, `compare_runs` returns no per-test delta. The live
  run's attempt 2 reported both tests as `deleted_test`; that is no longer possible.

Not measured: the prompt savings on a live run (no live tests were run).
