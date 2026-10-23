# Deterministic dependency-pin updates, smaller retry prompts

## Request

A the hosted provider run on djgpt (~/apifix-runs/djgpt-gptoss-2/) ended attempt 1 on the first file: whole-file mode sent environment.yml (an 11.5 KB conda export, not even installed because the repo has a pixi manifest) to the model, only to change openai==0.27.6, and the reply hit the output limit. Attempt 2 then aborted on a 413 for src/djgpt/prompt.py (prompt ~7,000 tokens against an 8,000 TPM limit).

Task: record this request verbatim in docs/plans/deterministic-pins.md, write the plan, and stop for my review.

Deterministic dependency-pin updates: for dependency findings, apifix edits the pin itself, with no LLM call, in every format it reads (requirements files, [project] dependencies, Poetry, Pipfile, setup.py/setup.cfg string literals, pixi pypi-dependencies, environment.yml pip: lists). Keep everything else in the file byte-for-byte (comments, quoting, line endings, extras, markers). Use the migration’s suggested pin (openai>=1.0,<2 for openai-v1). Also apply the existing knowledge about pins that hold openai back (e.g. raise an exact old typing_extensions pin), and say in the plan how far this should go.
Whole-file mode: dependency files whose only findings are pins handled by (1) are not sent to the model. Python files can see the updated pins as read-only context if useful. If a deterministic edit isn’t possible for some entry (an unusual format), fall back to sending the file as now, and record why.
Agent mode: decide whether the same pre-applied pins make sense there too.
Report and trajectory: list each pin apifix changed itself (“apifix set: environment.yml:158 openai==0.27.6 -> openai>=1.0,<2”).
The 413 on prompt.py: look at what made up the ~7,000-token prompt in attempt 2 (guide, feedback, previous diff, file) and propose what to trim on retries, without losing what the model needs. Function-level edits for big files are a separate, later feature; mention them only as an option.

List decisions for me at the end.

## Findings

From `~/apifix-runs/djgpt-gptoss-2/` and `~/apifix-runs/djgpt-gptoss-1/` (same repo, same model,
`openai/gpt-oss-120b`, 8,000 TPM). Token splits were counted with `o200k_base` (gpt-oss's base
encoding); they match the hosted provider's `input_tokens` to within the ~74 tokens of chat framing.

### The attempt-2 413 was environment.yml again, not prompt.py

- Attempt 1 failed on environment.yml, the first pending file, so `_whole_file_attempt` kept
  `pending[i:]`, which starts with environment.yml. Attempt 2 sent it first again, with
  "Your previous reply for environment.yml was rejected: the reply hit the output limit ..."
  (21 tokens) added.
- The numbers match exactly. Attempt 1's request was 6,999 tokens, and 6,999 + 21 = 7,020, the
  prompt in the 413. The 413 error says "less than the reply needs (~3,832)". 3,832 =
  ⌈11,495 / 3⌉, environment.yml's estimate. prompt.py's would be ⌈7,980 / 3⌉ = 2,660.
- **prompt.py was never sent in this run.** The trajectory makes this hard to see: a request
  that aborts is not recorded (attempt 2 has `files: []`), so neither the report nor the
  trajectory names the file. This plan fixes that (item 6).
- Attempt 1's 6,999 + 6,000 = 12,999 was accepted after one 53 s wait, although it is over
  8,000; the same request plus 21 tokens got a 413. the hosted provider's enforcement is not a simple
  prompt + `max_tokens` ≤ limit check, and these two data points do not explain it.

What the 7,020-token prompt was made of:

| Part | Tokens |
|---|---|
| System prompt: rules | 252 |
| Guide: Client 391, Resources 40, Responses 153, **Tests 645**, Dependencies 121, title 11 | 1,361 |
| environment.yml contents (158 conda build strings, ~180 pip pins, 150 of them `pyobjc*`) | **5,200** |
| Findings header, instruction | 112 |
| "Your previous reply ... was rejected" | 21 |
| Verifier feedback, previous diff | 0 (attempt 1 never reached verification and had no usable reply) |

The file was 74% of the prompt. No trimming would have rescued this request; not sending the
file at all does (items 1–3). The YAML conda export tokenizes at 2.2 characters per token, so
the chars / 3 reply estimate (3,832) was too low for it. The model's 6,000-token reply ran out
before the file was complete.

### prompt.py's real numbers (djgpt-gptoss-1, where it was sent)

| Request | Prompt tokens | Made of |
|---|---|---|
| Attempt 1 | 1,970 | system 1,492 (no Dependencies part) + file 1,757 + findings 149 + instruction 34 |
| Attempt 2 | 3,997 | the above + verifier feedback 187 + previous diff 304 |
| Attempt 2, check retry | 6,100 | the above + **the model's whole rejected reply (~2,040)** + the retry message |

gpt-oss used 2,137–3,828 reply tokens for this file, about the file's 1,757 tokens plus its
reasoning. Within a request, the check-retry turn is the biggest growth: the conversation
carries a full copy of the file the model just wrote. Across attempts, the worst case is
feedback at its cap (8,000 chars of test output, ~2,000–2,300 tokens) plus the previous diff at
its cap (3,000 chars, ~750 tokens). prompt.py's request would then be ≈ 6,500 tokens, leaving
no room for a ~2,700-token reply under 8,000. hosted-1's own 413 was tests/mocks.py: 2,841 of
its ~4,500 prompt tokens were read-only context (the full working copies of cli.py and
prompt.py).

### Other facts the plan relies on

- `check_dependency` makes a `dependency` finding (CERTAIN) only for entries whose clauses are
  entirely below 1.0. Positions come from the parsers. Requirements files and environment.yml
  have exact lines. TOML entries use `_locate_key` (the key inside the right table).
  `[project]` / setup.cfg strings use `_locate`, which takes the **first occurrence in the
  whole file**, so a comment with the same text above it gets the line wrong. Edits cannot
  rely on that.
- The pyproject finding's `old_pattern` is `openai = '==0.28'`, a Python repr, while the
  file has `openai = "==0.28"`. Report lines must quote the source text.
- openai 1.109.1 (the last release before 2.0; PyPI metadata checked today) requires:
  `typing-extensions>=4.11,<5`, `anyio>=3.5.0,<5`, `httpx>=0.23.0,<1`, `pydantic>=1.9.0,<3`,
  `distro>=1.7.0,<2`, `jiter>=0.4.0,<1`, `sniffio`, `tqdm>4`, and Python >=3.8. openai 1.0.0
  needed only `typing-extensions>=4.5`. So an exact `typing_extensions==4.5.0` makes pip fall
  back to an old 1.x without failing. That old openai then breaks on httpx 0.28 (`proxies`),
  which the guide and `failure_hints` already describe. The smoke check reports the repo's
  exact pins on openai's requirements (`SmokeResult.pins`). That is after the fact; nothing
  edits them today.
- `suspicious.check_dependencies` flags only new dependency names, and `scope.py` ignores
  dependency-pin changes. Pin edits and raised companion pins trigger neither.
- In djgpt the install plan reads `[tool.pixi.pypi-dependencies]` and ignores environment.yml
  (pixi manifest present). With `--sandbox-requirements`, the fix takes its own line for every
  package whose detected requirement changed, so editing the pixi pin is what changes the
  sandbox. Editing environment.yml changes only the PR.

## Plan

### 1. Pin edits (`engine/pin_edits.py`, new; stdlib only)

`plan_pin_edits(root, findings, migration) -> PinPlan` (pure: reads files, returns edits) and
`apply_pin_edits(work, plan)` (writes them). `PinPlan.edits: list[PinEdit]`, where each
`PinEdit` holds file, line, the exact old and new source text of the changed span, kind
(`migration` | `companion`) and why. `PinPlan.fallbacks: list[PinFallback]`, where each holds
file, line, entry and reason.

The edit for each `dependency` finding:

- **What changes:** only the version-spec characters. For a PEP 508 string (requirements,
  `[project]`/`optional-dependencies`/`dependency-groups`, setup.py, setup.cfg, environment.yml
  `pip:`), the clauses after the name and extras, up to `;`, `#`, a closing quote or the end of
  the line, are replaced with `>=1.0,<2`. The name, extras, spaces before the spec, the marker,
  the comment, the quotes and the line ending stay. For a `name = "spec"` table (Poetry, Pipfile,
  pixi `pypi-dependencies`), the string's contents are replaced, or the `version` value of an
  inline or sub-table. Quote style (`"` / `'`) is kept. Spacing *inside* the old spec
  (`>= 0.27, < 0.28`) is not kept: the new spec is written as `>=1.0,<2`.
- **Where:** each format gets its own locator, none of them `_locate`. Requirements and
  environment.yml use the parser's line. TOML files use candidate quoted strings / keys inside
  the table's span. setup.py uses the AST constant's exact span, and only for a plain
  single-line literal (no prefix, escapes, implicit concatenation or f-string). setup.cfg uses
  the lines of the option's value. Several identical entries are each edited.
- **Safety net, for every format:** splice into the decoded text, then re-parse both versions.
  The edit is kept only if the new parse differs from the old in exactly the intended entries,
  and those now have the intended clauses. TOML: the whole decoded document compares equal
  after substituting the expected values. setup.py: an AST dump equal apart from those
  constants. setup.cfg / environment.yml / requirements: every other entry is unchanged. Two
  more conditions: `migration.check_dependency` no longer flags the entry, and
  `dependency_file_problem` is still None if it was before. Any mismatch becomes a fallback,
  with the mismatch as its reason. Bytes are read and written as bytes, so CRLF, a BOM and the
  final newline survive.
- **Fallbacks (sent to the model as now, reason recorded):** a requirement continued with `\`,
  hash-pinned lines (`--hash`; see decision 7), TOML multi-line or escaped strings, non-literal
  setup.py values, a file that does not parse, a re-parse mismatch, and any file with
  non-dependency findings too (e.g. a setup.py that also calls openai; decision 2).

New migration data (protocol fields, filled in `openai_v1/rules.py`):

- `pin_spec = ">=1.0,<2"`. `DEPENDENCY`'s suggestion text is built from it, so the two cannot
  drift.
- `companion_pins`: normalised name → (openai's minimum, openai's upper bound), from the
  1.109.1 metadata above. The rules.py comment records the source and date. Entries:
  typing-extensions, anyio, httpx, pydantic, distro, jiter. tqdm and sniffio are left out: no
  repo realistically pins them below what openai needs.

### 2. Companion pins: how far it goes

- **Edited:** a pip entry for one of those six packages whose clauses exclude every version
  openai accepts for it. That is an exact pin outside openai's range (`typing_extensions==4.5.0`,
  `pydantic==3.0`), or a range entirely below openai's minimum (`httpx<0.20`).
- **Only in files that also declare openai.** In djgpt that means `pyproject.toml` and
  environment.yml's `pip:` list. A `docs/requirements.txt` pinning `typing_extensions` for
  Sphinx is left alone: changing it would be unrelated to the migration. Pins in other files that
  still hold openai back are caught by the smoke check (as today), which names them for the model.
- **Replacement:** `>=<openai's minimum>,<<next major of that minimum>>` (caret-style):
  `typing_extensions>=4.11,<5`, `pydantic>=1.9,<2`, `anyio>=3.5,<4`, `httpx>=0.23,<0.24`. This
  is the smallest move that satisfies openai, and it keeps a pydantic-v1 code base on v1.
  Alternatives in decision 1.
- **Not done:**
  - Packages further down the tree (`httpcore`, `pydantic-core`, `h11`, ...) are not touched.
    In a fully pinned file, raising httpx can clash with an exact `httpcore` pin. That shows
    up as an install failure, and the file then goes to the model with pip's output (existing
    path).
  - Conda entries are never edited: they are not installed, and an export's build strings
    cannot be edited correctly. The report names a conda entry for a companion package in a
    file apifix edited, e.g. "not changed: environment.yml:132 typing_extensions=4.5.0
    (conda entry)".
  - Lockfiles (`poetry.lock`, `Pipfile.lock`, `pixi.lock`) are not edited. The sandbox already
    drops lockfile pins that contradict a requirement. The report says "regenerate
    `pixi.lock`" when one exists next to an edited manifest.
  - `requires-python` is not touched. A repo on Python 3.7 gets an old 1.x from pip; the
    smoke check or the tests show it.
  - `httpx>=0.28` needs no edit: with `openai>=1.0,<2`, pip picks the newest 1.x, which works
    with it.

### 3. Whole-file mode

- `run()` applies the plan to the working copy before file selection (the baseline runs on the
  original repo, unaffected). A dependency file is left out of `wf_files` when all its findings
  were edited. It is recorded as kept from the start, like a dependency file the model's
  earlier attempt got accepted.
- The job guide is computed from the findings of the files actually sent. With no dependency
  file sent, the Dependencies part (121 tokens) drops out of the system prompt.
- **Python files get one line, not files**, in the system prompt (fixed per job, so it stays
  cacheable): "apifix has already set the openai pin (`openai>=1.0,<2`) in pyproject.toml and
  environment.yml; do not add or change dependency declarations." Read-only copies of
  dependency files would cost thousands of tokens and tell a Python file nothing it needs.
- **Fallback:** a file with any entry apifix could not edit is sent exactly as today (the
  original, no partial edits) with all its findings, so the model sees one consistent file. The
  reason is recorded per entry.
- **Later attempts:** `_dependency_problem` decides which dependency files are resent after a
  rejection. Today it implicates every dependency file on any install or smoke failure. From now
  on, an install or smoke failure implicates only files the fixed run's install plan read
  (`SandboxResult.dependency_sources`). A reason or remaining finding that names the file still
  implicates it. environment.yml next to a pixi manifest is therefore never resent because of
  sandbox problems it cannot have caused. A pinned file that is resent goes as now (the
  original, its pin-edited version as the previous diff, the dependency feedback), with the
  Dependencies guide part in that request's message, since the job's system prompt no longer
  has it.
- **Nothing left for the model:** when every finding was a pin apifix set, attempt 1 runs the
  verifier directly, with no preflight and no LLM call. A rejection then goes through the normal
  retry path.

### 4. Agent mode: yes, pre-apply too

Recommended. In agent mode a dependency file has to be read through `read_file`, and the read
stays in the conversation for every later request. For djgpt that would be ~5,200 tokens resent
on every turn. The edit itself (an exact replacement) is also where small models slip, e.g. by
copying the old string with the line-number prefix. `task_prompt` lists those findings under
"Already done by apifix (pins set; change them only if verification reports a dependency
problem)" instead of among the call sites. The model may still edit dependency files. Fallback
entries stay in the normal list. Unlike whole-file mode, partial edits are fine here: the model
edits the working copy, which already has them.

### 5. Report and trajectory

- Report, after the "Sandbox dependencies" lines:
  `- apifix set: environment.yml:158 openai==0.27.6 -> openai>=1.0,<2`
  `- apifix set: pyproject.toml:44 openai = "==0.28" -> openai = ">=1.0,<2"`
  `- apifix set: requirements.txt:12 typing_extensions==4.5.0 -> typing_extensions>=4.11,<5 (openai 1.x needs typing-extensions>=4.11)`
  `- apifix could not set: setup.py:9 (requirement is not a plain string literal); sent to the model`
  plus the conda and lockfile notes from item 2. Old and new are source text, with the line
  where the edit was made.
- Trajectory: `job.pin_edits` (file, line, old, new, kind, why) and `job.pin_fallbacks` (file,
  line, entry, reason). The edits are also in each attempt's diff, as part of the fix.

### 6. Recording the aborted request

When `_call_llm` aborts inside a whole-file request, the file's entry is still appended to the
attempt, with `error` set to the abort and `messages` as sent. Every whole-file entry gets
`prompt_parts`: character counts of rules, guide, feedback, findings, file, previous version,
context and conversation turns. With the server's prompt count from a 413, the next analysis
like this one is a lookup.

### 7. Check retries as fresh requests (step 1; steps 2–6 deferred, see Later)

Always on in whole-file mode (amended; first proposed only for requests over a stated limit).
Today the conversation keeps the model's full rejected reply (~2,040 tokens for prompt.py).
Instead, the file's request is sent again, with the rejected candidate as a diff against the
original and the error. `previous_version_note` already does exactly this for later attempts.
The shared prefix (system + feedback + file) stays the same.

**Function-level edits** (later, separate feature, not part of this plan) would be the next
lever. For a big file, the model would get only the functions with findings plus the imports,
and return replacements for those. That removes the "whole file in the prompt and again in the
reply" cost, which no trimming above can.

For djgpt on a hosted endpoint, items 1–3 alone mean both dependency files are set by apifix, attempt 1
sends cli.py, prompt.py, tests/mocks.py and tests/test_ci.py (all fitted in hosted-1), and the
system prompt is about 1,530 tokens (hosted-1's 1,492 plus the pin note) instead of 1,613.

### Tests (synthetic fixtures only)

- `test_pin_edits`: one case per format, all with extras, a marker, a comment and quote style.
  The formats: requirements, `[project]`, optional-dependencies, dependency-groups, Poetry
  (string, inline table, `[tool.poetry.dependencies.openai]` sub-table), Pipfile, pixi (top
  level, target, feature), setup.py, setup.cfg, environment.yml `pip:` (quoted/unquoted).
  Byte-for-byte checks: CRLF, BOM, no final newline, a same-text comment above the entry,
  duplicate entries. Fallbacks: `\` continuation, `--hash`, TOML escapes and multi-line strings,
  a concatenated setup.py string, a forced re-parse mismatch. Companion cases: exact pin below,
  exact pin above, a range below, an entry in a file without openai (untouched), a conda entry
  (report note only).
- `test_agent`: in whole-file mode, pinned files are not sent, the one-line note is in the
  system prompt, the guide has no Dependencies part, a fallback file is sent unchanged with its
  reason, and a pin-only job is verified with zero LLM calls. Narrowed `_dependency_problem`
  (a file outside `dependency_sources`). In agent mode, the task prompt has the "already done"
  section. The aborted request is recorded with `prompt_parts`. The trimming ladder against a
  fake provider that refuses above N: each step in order, the remembered limit, the stop
  message.
- `test_whole_file`: the fresh-request check retry, per-file guide parts, the reduced feedback
  and diff caps, context as diffs.
- `test_cli` / report: the `apifix set` / `could not set` lines and the trajectory keys.
- Docker: a synthetic repo with `requirements.txt` (`openai==0.28.1`,
  `typing_extensions==4.5.0`) and a pin-only finding set. The job reaches `verified` with no LLM
  call, and the smoke check passes on the raised pins.

### Docs

CLAUDE.md: a layout line for `pin_edits.py`; known limitations for pin edits (formats that
fall back, spec spacing normalised, companion scope, conda entries and lockfiles untouched),
whole-file (pinned files not sent, the trimming ladder, the remembered limit), and agent
("already done" pins). README: one paragraph on deterministic pins.

### Afterwards

Rerun djgpt on a hosted endpoint with the same `--sandbox-requirements` file and compare with hosted-1 and
hosted-2.

## Decisions for you

1. **Companion replacement form:** caret range from openai's minimum
   (`typing_extensions>=4.11,<5`, `pydantic>=1.9,<2`; my proposal), vs openai's own range
   (`pydantic>=1.9,<3`, which can move a v1 code base to pydantic 2), vs an exact pin at the
   minimum (`typing_extensions==4.11.0`, which keeps fully pinned files fully pinned but picks
   a version nobody tested with the rest of the file).
2. **Companion scope:** only files that also declare openai, only openai's direct requirements,
   pip entries only (my proposal). Widen to every dependency file the install plan reads?
3. **Whole-file fallback granularity:** a file with any entry apifix cannot edit (or with
   non-pin findings) goes to the model untouched, no partial edits (my proposal). The
   alternative is to send it with the editable pins already applied, as the file's new
   "original".
4. **Agent mode:** pre-apply, and list the pins as already done (my proposal). Or leave agent
   mode as it is?
5. **Pin-only jobs:** verify with no LLM call at all, and skip the preflight (my proposal)?
6. **When nothing fits after trimming:** stop the job, naming the file (my proposal, as today),
   or skip that file for the attempt and verify the rest, knowing its findings remain and the
   verifier will reject as incomplete?
7. **Hash-pinned requirements (`--hash`):** fall back to the model as your request says,
   although it cannot produce valid hashes either. The alternative is to leave the file
   untouched, report "update manually (hash-pinned)", and not send it.
8. **Trimming only on a stated limit (my proposal), or some steps always?** The best
   candidates for "always" are step 1 (check retry as a fresh request; it saves a file's worth
   of tokens on every retry, but changes what small local models see) and step 2 (per-file
   guide parts; the system prompt then has up to 3 variants per job instead of 1, which still
   caches per variant).
9. **Remembered limit with a calibrated pre-check (my proposal), or reactive only?** Reactive
   only means trimming after each 413. That costs nothing in tokens but takes one extra round
   trip per oversized request.

## Later: smaller prompts on a stated limit (item 7, steps 2–6; deferred)

The ladder as proposed. Step 1 is built, always on (item 7); steps 2–6 and the remembered
limit are deferred.

Normal requests stay as they are. The steps below run only when a request does not fit a
per-minute limit the server has stated in this job. They are applied in order, each only if
still needed, cheapest loss first:

1. **A check retry becomes a fresh request.** Today the conversation keeps the model's full
   rejected reply (~2,040 tokens for prompt.py). Instead, send the file's request again, with
   the rejected candidate as a diff against the original and the error. `previous_version_note`
   already does exactly this for later attempts. The shared prefix (system + feedback + file)
   stays the same.
2. **Only the guide parts this file needs**: no Tests part (645 tokens) for a non-test Python
   file, only Dependencies for a dependency file. The system prompt is rebuilt for this request.
3. **Less test output in the feedback:** 3 tests × 800 characters, 2,400 total (instead of
   10 × 2,000, 8,000). Every reason line and every failure hint stays.
4. **Read-only context (test files only):** full working copies become their diffs against the
   original (3 lines of context), then are dropped, last first. Dropped files are recorded in
   `context_left_out`.
5. **Previous diff** capped at 1,200 characters instead of 3,000.
6. **`max_tokens` lowered** to fit, as today (at least the reply estimate).

Never trimmed: the rules, the file itself, its findings, the verifier's reasons, the hints,
the instruction.

Knowing the limit: today `_fitting_max_tokens` learns the limit from a `RequestTooLarge` and
forgets it. Proposed: keep the stated limit for the rest of the job. On the refused request, it
uses the server's own prompt count to decide how many steps to apply. On later requests, it
pre-checks with an estimate calibrated on the job's own responses (the highest
`input_tokens / prompt chars` ratio seen, e.g. 2.6 for the YAML file, ~4.4 for Python), so they
are trimmed before being sent rather than refused first.

When even the full ladder does not fit: the job stops as today (`provider_error`). The message
now names the file and its `prompt_parts`, and suggests a model with a higher limit. The
alternative is decision 6.

## Amendments

Approved with reduced scope. Record these decisions verbatim in the plan’s Amendments section, then build.

Build: items 1–6, and from item 7 only step 1 (a check retry becomes a fresh request with the rejected candidate as a diff against the original and the error), always on, in whole-file mode.

Decisions:

Companion pins use openai’s own range for that package (e.g. typing_extensions>=4.11,<5, httpx>=0.23,<1, anyio>=3.5,<5), except pydantic: keep the repo’s current major (a pin on 1.x becomes pydantic>=1.9,<2; on 2.x, pydantic>=2,<3).
2–6. As proposed.
Hash-pinned requirement files are not sent to the model: report “apifix could not set: <file>:<line> (hash-pinned; update manually)” and record it as a fallback that isn’t sent.
Step 1 always on; steps 2–6 of item 7 are deferred (keep them in the plan as a later section).
Deferred with steps 2–6.

Use synthetic fixtures only. Run the fast suite and ruff, and the Docker suite if the network allows, then commit. Don’t run the live tests.

## Built

Items 1–6 and item 7 step 1, with the amended decisions. Changes from the plan as written:

- **Scanner:** requirements-file lines are now read without per-requirement options
  (`--hash=...`) and a trailing `\`. Before this, a hash-pinned file (pip-compile
  `--generate-hashes` output) produced no finding at all, so decision 7 could never apply.
- **Companion scope:** "files that also declare openai" is implemented as "files where apifix
  sets the openai pin". The two are the same for a 0.x pin. A file that already allows openai
  1.x has no finding and is not touched.
- **Hash-pinned files:** besides not being sent, a job whose remaining finding is in one is not
  retried. Nothing the model may edit can resolve it, so every further attempt would be
  rejected the same way. In agent mode the task prompt lists the entry under "Left for a manual
  update".
- **Nothing to resend:** whole-file mode stops after a rejection that implicates no file it may
  send. This happens when a job had only pins and the verification names nothing editable;
  without the stop, the job would verify the same tree up to four times.
- **Aborted requests:** the abort reason ends with "(request for <file>)" (decision 6), and the
  entry keeps `prompt_parts` and the message that was sent.
- **Fresh check retries:** "Keep these fixed" moved from the old retry message into
  `previous_version_note`. `check_retry_message` is gone. Code-block nudges still continue the
  conversation, since there is no candidate to show as a diff.

Tests: `tests/test_pin_edits.py` (every format, byte-for-byte cases, fallbacks, companion and
pydantic cases, plan application per mode), new agent tests (pin-only jobs in both modes with
no model contact, the agent task prompt's "already done" and "manual update" sections, partial
agent edits, hash-pinned whole-file job, narrowed resend rule, aborted request recorded), and
`tests/integration/test_pins_docker.py`. That Docker test is a pin-only repo with
`typing_extensions==4.5.0`, verified in a real sandbox with the smoke check passing on openai
1.x, in both modes.
