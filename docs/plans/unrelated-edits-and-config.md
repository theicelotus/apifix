# Unrelated edits in whole-file mode, dropped openai module config

Requested 2026-10-22, for review before building. The request is verbatim.

## Request

Context: Read CLAUDE.md. A the hosted provider run on MattOates/djgpt (~/apifix-runs/djgpt-gptoss-3/, see fix.diff) ended verified_weak (0 of 4 changed lines covered), and a manual review found two problems no check caught:

src/djgpt/cli.py:69: the model replaced openai.api_key = openai_api_key (a CLI option) with a comment, so a key passed on the command line is silently ignored. The scanner’s client_config hint (“Pass it to the client: openai.OpenAI(api_key=...)”) encouraged removing it, but the client is created in another module.
src/djgpt/prompt.py:193: inside a prompt string unrelated to the migration, the model changed “how likely” to “how such likely”. Whole-file mode let an unrelated edit through.

Task: record this request verbatim in docs/plans/unrelated-edits-and-config.md, write the plan, and stop for my review.

Unrelated-edit check (whole-file mode): compare each candidate with the original and find changed regions that aren’t related to the migration: not on or near a finding’s line, not import changes, not the code the migration needs (e.g. the lines around a migrated call or response access). Propose the exact rule, especially for string literals, comments and docstrings, which are where silent damage hides. Propose whether such hunks should be rejected through the existing check retry (“line 193 changed text unrelated to the migration; keep it exactly as in the original”), or reverted automatically by apifix, and the risks of each. Test it against djgpt’s real diff: line 193 must be caught, and none of the legitimate changes.
Global config in openai 1.x: update the scanner hint and the guide for openai.api_key / organization / api_base assignments: never drop a configured value. Either keep the assignment and use the module-level client (openai.chat.completions.create(...), which reads openai.api_key), or pass the value to every OpenAI(...) that needs it. Say which to prefer when the assignment is in a different module from the call.
A check for dropped config: reject a candidate that removes an assignment to openai.api_key / organization / base_url / api_base whose value isn’t a literal (e.g. a parameter or env lookup) unless the same value is now passed to a client or still assigned. Message in the retry: what was removed and the two correct options.
Say whether verified_weak results should also print a short “review these lines by hand” list in the report (the changed lines no test ran).

Use synthetic fixtures modelled on the two patterns (don’t copy djgpt’s code beyond what the MIT license allows; short snippets are fine). List decisions for me at the end.

## Findings

### The run

- Both problems also occur in `djgpt-gptoss-1`: the same model (`openai/gpt-oss-120b`) made the
  identical `how  likely` -> `how such likely` edit, and also replaced `openai.api_key =
  openai_api_key` with a comment. So these are tendencies of this model, not one-offs. The
  original has a double space there (`how  likely`), and the model "repairs" it.
- Numbering: the prompt line is 190 in the original and 193 in the fix. The model is shown the
  original, so retry messages must use the original's numbers. The report uses the fix's.
- `openai_api_key` is a typer option: `Option(prompt=True, envvar="OPENAI_API_KEY")`, defaulting
  to `getenv("OPENAI_API_KEY")`. A key from the environment still works after the fix. A key
  passed as `--openai-api-key` or typed at the prompt is silently dropped.
- **Keeping the assignment would not have been enough.** prompt.py now builds `openai.OpenAI()`,
  and that constructor never reads `openai.api_key` (see below). With cli.py:69 left untouched,
  the CLI key would still have been ignored. So the fix broke the key twice, and a check on
  cli.py alone cannot catch it (item 3, check B).
- Why the verifier saw nothing: changed-line coverage counts lines of the fixed file. cli.py:69
  is now a comment, so it is not executable, and cli.py is missing from the report's "not
  executed" list (prompt.py:132,133,140,193). Coverage cannot see removed code at all. That
  bears on item 4.

### openai 1.x behaviour (checked against 1.109.1 in a scratch venv, not from memory)

| Code | Result |
|---|---|
| `openai.api_key = "k"; openai.OpenAI().api_key` | raises `OpenAIError` (no key): `OpenAI()` reads only its arguments and `OPENAI_API_KEY` |
| module client (`openai.chat.completions...`), key changed after first use | uses the new key: `_ModuleClient.api_key` is a property that returns the module global |
| `openai.api_base = url`, then the module client | request goes to `https://api.openai.com/v1/`: nothing in 1.x reads `api_base` |
| `openai.base_url = url`, then the module client | `url` is used |
| `OPENAI_API_BASE` env var (0.x name) | ignored; 1.x reads `OPENAI_BASE_URL` |
| `mock.patch("openai.OpenAI")`, then the module client | not affected: the module client is `_ModuleClient`, built directly |
| `mock.patch("openai.chat.completions.create")` with no key | raises at patch time: getting `openai.chat.completions` builds the client, which needs a key |
| same, with `OPENAI_API_KEY` set | works; the code's call returns the mock |
| `mock.patch("openai.resources.chat.completions.Completions.create")`, no key, module client call | raises: the module client is still built on first use |

So 1.x keeps `openai.api_key` / `organization` / `project` / `base_url` (not `api_base`) for the
module-level client only. Tests of code that uses the module client need a key in the test
environment, whatever they patch. djgpt's tests already set `OPENAI_API_KEY` to a dummy value,
and apifix's sandbox sets a placeholder because the repo's CI does.

### Prototype against the real diffs

I prototyped the rule in item 1 (a scratch script, not committed) and ran it over every Python
file in the six real fix diffs that touch Python files (djgpt-gptoss-1/3, resume-2,
resume-gptoss-1, resume-gptoss-1-verify, resume-gptoss-2; the others change only dependency files).

| Run | Caught | Legitimate hunks kept |
|---|---|---|
| djgpt-gptoss-3 | prompt.py:190 (fix: 193) `how such likely` -> **reverted** | all 13 |
| djgpt-gptoss-1 | prompt.py:190, the same edit -> reverted | all 15 |
| resume-2 (qwen 7b) | testapp.py:118-121: **deleted `if __name__ == '__main__': unittest.main()`** | all 8 |
| resume-gptoss-1 | app.py:22 **inserted an 80-line `_create_dummy_pdfs()` that writes PDF files into the repo at import**; app.py:54-70 refactored base64/PDF code unrelated to openai; testapp.py: unused mock value dropped (string rule) | all 14 others |
| resume-gptoss-1-verify, resume-gptoss-2 | (-1-verify only) testapp.py: the same unused mock value dropped | all 13 |

The first version of the "related names" part let the `_create_dummy_pdfs` insertion through
because it uses `os` and `content`. The rule below takes names only from bindings and attribute
accesses, and with that it is caught. One known miss: hosted-1 replaced `import openai` with a
try/except shim that stubs the library. That shim uses library names, so it counts as related
(see limitations).

The dropped-config prototype flags djgpt's cli.py:69 (both runs) and prompt.py's new
`openai.OpenAI()` (check B). It does not flag resume-analyzer's removed
`openai.api_key = os.environ.get("OPENAI_API_KEY")`, which 1.x reads itself; that removal is
correct, and three of the resume runs make it. Check B also flagged a mocked `OpenAI()` in
djgpt's tests/test_ci.py, so B skips test files.

## Plan

### 1. Unrelated-edit check (`engine/unrelated.py`, new; pure, stdlib only)

Python files only, in whole-file mode, test files included (resume-2's deletion was in a test
file). Input: the original, the candidate, the file's findings, the library's module names.

**Definitions.** All positions are on the original unless stated.

- *Hunks*: `difflib.SequenceMatcher(autojunk=False)` line opcodes other than `equal`, the same
  diff that `diffing.changed_lines` uses.
- *Region* of a finding: the innermost statement holding the finding's line. A simple statement
  counts with its full span, so a 9-line mock dict assignment is one region. A compound
  statement counts with its header only: decorators to the line before the first body
  statement, which covers `with patch(...) as m:`, `except openai.error.X:` and a decorated
  `def`. `except` handlers count as statements.
- *Near*: a replace or delete hunk whose original lines come within `PROXIMITY = 3` lines of a
  region. An insertion counts only if it falls strictly inside a region's span. Insertions get
  no proximity because an 80-line insertion right next to a finding is exactly the risk.
- *Import hunk*: every non-blank, non-comment line on both sides belongs to an `import` /
  `from ... import` statement.
- *Text-only hunk*: the two sides have the same token streams once these are set aside:
  `COMMENT` and `NL` tokens dropped, `STRING` (and f-string part) contents replaced by a
  placeholder, whitespace ignored. `INDENT`, `DEDENT` and `NEWLINE` stay. Both whole files are
  tokenized, and a hunk takes every token that overlaps its lines, so a hunk inside a
  triple-quoted string still has its whole string token. A whitespace-only reflow is text-only.
- *Exempt strings*: string constants on a finding's own lines (`line..end_line`), such as mock
  target strings (`"openai.ChatCompletion.create"`), the `"azure"` of an `api_type` line and
  removed config values. For a dict-literal finding (a 0.x-shaped mock response), only its
  keys (`"choices"`, `"message"`, `"content"`) are exempt. Its values (the mock content) are
  protected.
- *Migration names*: (a) the library's module names and the names the candidate imports from
  it (`openai`, `OpenAI`, ...); (b) names bound on either side of a region's code hunks or the
  candidate side of an import hunk: assignment / `for` / `with ... as` targets, `def` / `class`
  names and parameters, imports, `except ... as` names (e.g. `client`, `response`,
  `mock_client`, `MockOpenAI`); (c) attribute names accessed (`.x`) on the candidate side of a
  region's code hunks (`.chat`, `.completions`, `.create`, `.choices`, `.message`,
  `.content`). `self`, `cls`, keywords and builtins never count.

**Rule, per hunk:**

| Hunk | Outcome |
|---|---|
| import hunk | kept |
| text-only, touches an exempt string | kept (mock targets, response keys) |
| text-only, only comments/whitespace differ (strings equal), near a region | kept (comments describing migrated code) |
| any other text-only hunk (a string, docstring or comment changed away from the migration; a changed string inside a region; inserted or removed comment lines far away) | **reverted by apifix** |
| code hunk near a region | kept |
| code hunk elsewhere that uses a migration name (an identifier from (a)/(b), or an attribute from (c)) | kept (renamed mocks, `yield MockOpenAI`, `client` threaded through a signature, `get_client()` helpers, response access the scanner missed in another function) |
| any other code hunk (changed, removed or added) | **rejected** through check retry |

**String rule**, applied after the reverts: every non-exempt string constant of the original
(docstrings and f-string parts included) must still be in the candidate, compared as a
multiset of values over the whole file, so moved code passes. A missing one is an error, unless
it sits inside a hunk already rejected (one message, not two). This catches strings the
migration has no reason to change in hunks that also change code: `model="gpt-3.5-turbo"` ->
`"gpt-4o"` on the line after a migrated `create(`, or mock content altered while the dict
becomes a `SimpleNamespace`.

**Retry messages** (no trailing period, as `check_candidate` expects; the excerpt is the first
non-blank line, at most 80 characters):

```
original line 54: changed code unrelated to the migration ("file_bytes = BytesIO(base64.b64decode(file_data))"); keep it exactly as in the original
original line 118: removed code unrelated to the migration ("if __name__ == '__main__':"); keep it exactly as in the original
after original line 21: added code unrelated to the migration ("def _create_dummy_pdfs():"); remove it, make only the changes the migration needs
original line 132: the string 'gpt-3.5-turbo' is missing; keep string literals exactly as in the original unless they name the old API
```

`error_key` also strips `original line N: ` and `after original line N: `, so `keep_fixed`
matches the same problem on another line.

**Reverts.** The candidate's lines of each reverted hunk are replaced with the original's,
bottom up. The result must parse. If it does not, nothing is reverted and each of those hunks
becomes a retry error instead (`original line N: changed text unrelated to the migration ...;
keep it exactly as in the original`, the wording in the request). Reverts are recorded per
reply (`reverted: [{"line", "fixed_line", "old", "new"}]`) and per file entry. The report gets
one line per hunk, like `apifix added:`:

```
- apifix reverted: src/djgpt/prompt.py:193 (text changed outside the migration: "...based on how such likely we are..." restored to the original)
```

**Revert vs retry, the risks.**

- *Retry* costs one request per file (on a hosted free tier, a ~7,000-token request against an
  8,000 TPM limit). Its failure mode is losing a good file: whole-file mode allows one check
  retry, and a second failure ends the attempt. Small models are bad at reproducing exact text.
  They strip trailing spaces inside strings (djgpt's prompt has them on every line) and repeat
  "fixes" (the double space was "fixed" in two separate runs). Its strength is that the model
  sees the rule, so for *code* a mistaken flag costs one request, not a silent change.
- *Revert* is free and cannot lose the file. Its risks: (a) undoing a text edit the migration
  needed. Strings on finding lines are exempt, comments near migrated code are kept, so what is
  left is prose such as a docstring that mentions the old API, and putting stale prose back is
  harmless. (b) A hunk boundary that does not parse after the revert: re-parsed, with retry as
  the fallback. (c) The model never learns. Nothing in the job depends on that, and a later
  attempt's diff is computed after the revert. Reverting *code* would be dangerous: a code hunk
  the rule does not understand may be needed, and reverting it can break the fix silently. A
  rejection at least says what happened.

**Recommendation:** revert text-only hunks, reject code hunks through check retry (the hybrid
above).

**Order in `check_candidate`:** syntax -> reverts -> `add_missing_imports` (its lines form import
hunks) -> the existing checks -> `unrelated_edits` (code hunks + strings) -> `config` (item 3).
New check names in `checks`: `unrelated_edits` (`n/a` for non-Python files); `CheckResult` gets
`reverted`. A reverted candidate that now equals the original fails `changed` as usual when the
file has [certain] findings.

### 2. Global config: scanner hint and guide

**Scanner (`python_checks.py`, `rules.py`).** Assignments to `openai.api_key` /
`organization` / `api_base` / `base_url` (aliases resolved, e.g. `oa.api_key = ...`) are
reported on the whole statement (`old_pattern = "openai.api_key = openai_api_key"`). The
suggestion depends on the value:

| Value | Suggestion |
|---|---|
| `None`, or an env read that 1.x does itself (`os.getenv("OPENAI_API_KEY")`, `os.environ.get(...)`, `os.environ[...]`, with no default; `OPENAI_ORG_ID` for organization, `OPENAI_BASE_URL` for base_url) | `1.x reads OPENAI_API_KEY itself: this line can go (OpenAI() and the module-level client both read it)` |
| anything else (parameters, other env vars such as `OPENAI_API_BASE`, settings, literals) | `Never drop this value: keep the assignment and call the module-level client (openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: openai.OpenAI(api_key=openai_api_key)` |
| `api_base`, any value | `1.x ignores api_base: assign openai.base_url = <value> and use the module-level client, or pass openai.OpenAI(base_url=<value>); never drop it` (CERTAIN, as now) |

Reads of the attribute (`if not openai.api_key:`) keep a [likely] finding with a neutral hint:
`1.x keeps openai.api_key for the module-level client only; openai.OpenAI() does not read
it`. Azure files keep their own suggestions, with "never drop the value" added.

**Repo-level pass** (new `Migration.adjust_findings(findings) -> list[Finding]`, called once at
the end of `scan()`; the walker is unchanged). The scanner is per file, so prompt.py's `fix:`
line, "Use client.chat.completions.create(...) with client = openai.OpenAI()", knew nothing of
cli.py. When a non-test, non-Azure file has a "Never drop" assignment, openai-v1 rewrites:

- api_call suggestions in non-test files:
  `Use openai.chat.completions.create(...): the module-level client, which reads openai.api_key
  (set in src/djgpt/cli.py:69); the response is a typed object, not a dict`. Async calls:
  `Use await client.chat.completions.create(...) with client = openai.AsyncOpenAI(api_key=openai.api_key)
  created where it is used` (1.x has no module-level async client).
- the assignment's suggestion, when its file has no api_call findings and other files do:
  `... The API calls are in src/djgpt/prompt.py: keep this assignment.`
- string mock-target findings in test files:
  `... patch "openai.chat.completions.create" (the code uses the module-level client; the test needs OPENAI_API_KEY or openai.api_key set)`.

**Guide (`guide.md`).** New part `client.config`, selected by a finding whose suggestion holds
"Never drop" (selector in `rules.GUIDE_SELECTORS`). So repos like resume-analyzer, which
only set the key from `OPENAI_API_KEY`, do not pay for it:

````markdown
<!-- part: client.config -->
- **Never drop a configured value** (`openai.api_key = <value>`, `organization`, `api_base`).
  `OpenAI()` reads only its arguments and the environment (`OPENAI_API_KEY`, `OPENAI_ORG_ID`,
  `OPENAI_BASE_URL`), never `openai.api_key`. Either keep the assignment and call the
  module-level client, which reads `openai.api_key` / `organization` / `base_url` for each
  request:

  ```python
  openai.api_key = key  # unchanged
  resp = openai.chat.completions.create(model=m, messages=msgs)
  ```

  or pass the value to every client: `OpenAI(api_key=key)`. When the assignment is in one module
  (e.g. a CLI that takes `--api-key`) and the API calls are in another, keep the assignment and
  use `openai.chat.completions.create(...)` at the call sites. 1.x ignores `openai.api_base`:
  assign `openai.base_url` instead. An assignment from the variable 1.x reads itself
  (`os.getenv("OPENAI_API_KEY")`) can go; 0.x's `OPENAI_API_BASE` is not one (1.x reads
  `OPENAI_BASE_URL`). Async code: `AsyncOpenAI(api_key=openai.api_key)` where it is used.
- Tests of code that uses the module-level client: patching `openai.OpenAI` does not reach it.
  Patch `openai.chat.completions.create`, with a key set (`OPENAI_API_KEY` or
  `openai.api_key = "test"`): the module client is built on first use, and that needs a key.
````

The `client` part's sentence "Pass `api_key=` only where 0.x code set a key from somewhere else"
gets "(see below)" when `client.config` is selected, and stays as is otherwise.
`failure_hints` for "api_key client option must be set" gains: "If the code uses the
module-level client, set a placeholder key in the test (OPENAI_API_KEY, or openai.api_key =
"test")." `code_checks`' mock suggestion for a module whose call is a direct library call
(`openai.chat.completions.create(...)`) is checked during the build to name that target. The
existing `api_modules` path should already do this; a test covers it.

**Which to prefer when the assignment is in a different module from the call: keep the
assignment and use the module-level client.** It is a two-line change: the assignment stays,
and each call site uses `openai.chat.completions.create`. The value is read when each request is
sent, so the order of import and configuration does not matter. Passing the value to every
`OpenAI(...)` instead means threading it from the CLI through constructors into another module.
Whole-file mode edits one file at a time and cannot do that consistently. Passing the value is
preferred when the assignment and the calls are in the same function or module and a client is
created there anyway (`openai.api_key = key` + `openai.ChatCompletion.create(...)` in one
function -> `OpenAI(api_key=key)`), and for async code.

### 3. Dropped and ignored config checks (`migrations/openai_v1/config_checks.py`)

These are migration-specific, so they live in the migration, behind one new protocol method:
`Migration.rewrite_checker(root, findings) -> Callable[[path, original, candidate], list[str]]
| None`. It is built once per job from the original repo (its config assignments, the files
with api_call findings) and passed into `check_candidate` as `rewrite_checks`. Errors use the
existing check retry; the check name is `config`. Non-test files only (a test's
`openai.api_key = "test"` setup is judged by its test run).

**Check A (dropped).** For each assignment in this file's original to `api_key` /
`organization` / `api_base` / `base_url`, whose value is not `None` and not an env read that
1.x does itself (table in item 2), the candidate must either:

- assign the same value (`ast.dump` equal) to the 1.x name. For `api_base` that is
  `openai.base_url`: `openai.api_base = <same>` is an error ("1.x ignores api_base"), or
- pass the same value as the matching keyword (`api_key=`, `organization=`, `base_url=`; Azure:
  `azure_endpoint=` for `api_base`) to a client constructor (`OpenAI`, `AsyncOpenAI`,
  `AzureOpenAI`, `AsyncAzureOpenAI`, through any alias) in this file.

Message (second sentence chosen by where the calls are):

```
original line 69: removed `openai.api_key = openai_api_key`, so that key would be silently ignored in 1.x. Keep the assignment and call the module-level client (`openai.chat.completions.create(...)` reads openai.api_key), or pass the value to every client (`openai.OpenAI(api_key=openai_api_key)`). This file makes no API calls (they are in src/djgpt/prompt.py): keep the assignment
```

**Check B (ignored), which the request did not ask for.** Item 1 of the findings shows why it
is needed: keeping the assignment is not enough when a call site switches to `OpenAI()`. For a
client constructor call in the candidate that the original did not have, in a non-test,
non-Azure file, while the original repo has a check-A assignment in a non-test file, the call
must pass that keyword (any value) or `**kwargs`:

```
line 132: `openai.OpenAI()` does not read openai.api_key, which src/djgpt/cli.py:69 sets from `openai_api_key`, so that key would be ignored. Call the module-level client instead (`openai.chat.completions.create(...)`, no client object), or pass it: `openai.OpenAI(api_key=openai.api_key)`
```

B uses the *original*'s assignments, so the order in which whole-file mode sends the files
does not matter. A then requires the assigning file to keep its assignment.

Known false positives (documented): the value passed through a local variable (`key =
openai_api_key; OpenAI(api_key=key)`, which fails A), and a repo with two deliberately
different clients (fails B). Both get a message that names the exact accepted forms.

### 4. "Review by hand" list: yes, per hunk, not per line

Recommended for `verified_weak` and `unverified` results that change non-test Python files.
In djgpt, the existing per-line "not executed" list (prompt.py:132,133,140,193) left out the
real bug, cli.py:69, because a removed statement or one replaced by a comment has no
executable line. So the list is built from hunks:

```
### Review by hand

No test executed these changes:

- `src/djgpt/cli.py:69`: removed `openai.api_key = openai_api_key`; added `# The OpenAI client should be configured ...`
- `src/djgpt/prompt.py:131-133`: `response = openai.ChatCompletion.create(` -> `client = openai.OpenAI()` (+2 lines)
- `src/djgpt/prompt.py:140`: `gpt_text = response["choices"][0]["message"]["content"]` -> `gpt_text = response.choices[0].message.content`
```

- A hunk is listed when none of its fixed-side executable lines ran. Hunks with no executable
  fixed-side line (removals, code turned into a comment, edits inside a string) are always
  listed, since coverage cannot vouch for them.
- First line of each side, at most 100 characters; at most 15 hunks, then "... and N more
  (see fix.diff)". Fix-side line numbers, as in the diff.
- The trajectory gets `verification.review_by_hand` (the same entries).
- Code: `diffing.changed_lines` also returns hunks (`FileChange.hunks`);
  `ChangedLineCoverage.per_file` keeps the executed set per file; `report_section` renders the
  list.

### 5. Not in this step

The unrelated-edit and config checks in agent mode and `apifix verify`. Both are pure
functions and could run in the verifier: unrelated edits as `warn`, dropped config as
`reject`. Agent mode has the same risk, but the request is scoped to whole-file mode. This is a
candidate for the next step. Dependency files are not covered by item 1 either (pin edits are
apifix's own; model-edited dependency files keep the strict well-formedness check).

### Tests (synthetic fixtures; djgpt's code is not copied)

New fixture repo `tests/fixtures/split_config/`, modelled on the two patterns:

- `voicebot/cli.py`: `def main(api_key: str = os.getenv("VOICEBOT_OPENAI_KEY"), base_url: str |
  None = None)`, setting `openai.api_key = api_key` and `openai.api_base = base_url`, then
  calling `Assistant().reply(...)`.
- `voicebot/assistant.py`: a class with a multi-line `SYSTEM_PROMPT = """..."""` that has
  trailing spaces and a double space (`rate how  well`), a docstring, comments, and an
  unrelated helper; `openai.ChatCompletion.create(model="gpt-3.5-turbo", ...)`; dict response
  access.
- `tests/test_assistant.py`: `mock.patch("openai.ChatCompletion.create")` with a 0.x dict
  response, a test that asserts the mock was not called, and an `if __name__ == "__main__":`
  block.

Candidates are built in the tests from a hand-written good migration with `str.replace` edits.

- `tests/test_unrelated.py`:
  - Reverted: one word inserted in `SYSTEM_PROMPT` (the djgpt pattern), with both line numbers
    recorded; trailing spaces stripped inside it; a docstring rewritten; a comment far away
    reworded or removed; comment lines inserted far away.
  - Kept: client creation inserted before the call, the call and response access rewritten,
    imports added/removed/reordered, comments reworded within 3 lines of a finding, mock target
    strings and response keys on finding lines, `yield`/`assert_not_called` on the renamed
    mock, `get_client()` inserted at module level, a `client` parameter added to a signature,
    `data["choices"]` -> `data.choices` in another function, a whitespace reflow of a call near
    a finding.
  - Rejected: the `__main__` block removed; an unrelated function refactored; an unrelated
    helper inserted (one that writes files); `model="gpt-3.5-turbo"` changed inside the
    migrated call (string rule); mock content altered; an unused mock value dropped.
  - Mechanics: a revert that does not parse falls back to an error; exact messages; one message
    for a string inside a rejected hunk; `error_key` on the new prefixes.
- `tests/test_openai_config.py`:
  - Check A: a parameter value dropped (error naming both options and the calling file); kept;
    passed to `OpenAI(api_key=...)`; passed through a variable (documented error); the
    `OPENAI_API_KEY` env read dropped (ok); an `OPENAI_API_BASE` env read dropped (error);
    `api_base` kept (error), renamed to `base_url` (ok); literals (decision 6); an alias
    (`import openai as oa`); test files not checked.
  - Check B: a new `OpenAI()` in a non-test file (error naming cli.py's line);
    `OpenAI(api_key=openai.api_key)`, the module-level client and `**kwargs` are ok; test files
    and exempt values do not trigger it.
- `tests/test_scanner.py`: statement-level assignment findings and the suggestion per value
  class; reads; Azure unchanged; the repo pass (api_call, async, the "calls are in" note, test
  mock targets); guide part selection.
- `tests/test_whole_file.py`: check order (revert before auto-imports), `checks` /
  `reverted` recorded.
- `tests/test_agent.py`: a whole-file replay of both patterns on the fixture. The first replies
  drop the key, switch to `OpenAI()` and change the prompt word: the word is reverted with no
  retry, and A (cli) and B (assistant) go back through check retry. The second replies keep the
  assignment and use the module-level client: accepted. Report lines and trajectory checked.
- `tests/test_verifier.py`: the review list includes a removal, code turned into a comment and a
  string edit; leaves out executed hunks; caps at 15; appears only for the chosen verdicts.
- Docker (`-m docker`): the fixture's good migration (module-level client, tests that set a
  placeholder key and patch `openai.chat.completions.create`) passes in the sandbox with real
  openai 1.x. This checks the guide's test advice against the library itself.

After building, I re-run the real-diff check locally (djgpt-gptoss-1/3 and the four resume runs,
through the new module) and record the result under "Built". It must catch exactly what the
prototype caught.

### Docs

CLAUDE.md:
- Layout: `unrelated.py`, `migrations/openai_v1/config_checks.py`, `adjust_findings`.
- New limitations: the rule and its known misses (library-name shims, renamed strings that
  happen to occur elsewhere, false positives listed above), config checks (non-test files
  only, no Azure for B, values compared by AST), the review list.
- Scanner: statement-level config findings.

Guide as above.

## Decisions for you

1. **Text-only unrelated hunks: reverted by apifix** (recommended), not sent back. Strings,
   docstrings and comments away from the migration go back to the original, and the report
   lists each one. The alternative is check retry for these too.
2. **Unrelated code hunks: rejected through check retry** (recommended). The alternative is a
   warning only. In the real diffs, retry would have caught the `__main__` deletion and the
   PDF-writing insertion, with no false positives.
3. **Comments**: free near migrated code (within 3 lines of a finding's statement), reverted
   elsewhere. String literals are protected everywhere except on finding lines.
4. **Proximity 3 lines** for changed/removed lines; **none for insertions** (only inside a
   region's statement, or through migration names).
5. **Mock-response dict findings: only the keys are exempt**, so mock content is protected.
   This flags resume-gptoss-1's harmless removal of a mock value its test never used (one retry).
   The alternative is to exempt the whole dict.
6. **Literal config values (deviation from the request): check them too, except `None`.**
   Dropping `openai.api_base = "http://localhost:8000/v1"` sends requests, and the key, to
   api.openai.com, and dropping `openai.api_key = "EMPTY"` (the usual 0.x local-server pattern)
   makes `OpenAI()` raise. Your rule exempts literals.
7. **Env reads 1.x does itself (deviation from the request): exempt.** The request lists "env
   lookup" as a checked value, but `os.getenv("OPENAI_API_KEY")` is read by 1.x itself, and
   the guide tells the model to drop it. Checking it would reject the correct change in three
   resume-analyzer runs. Other variables (`OPENAI_API_BASE`, `MY_KEY`) stay checked.
8. **Check B (ignored config) is added**: a new `OpenAI()` in a non-test file must pass the
   value the repo configures elsewhere. Without it, "still assigned" lets the djgpt bug through.
9. **Preference across modules: keep the assignment and use the module-level client.** The
   scanner's repo-level pass rewrites the call sites' suggestions to say so (a new
   `Migration.adjust_findings` hook).
10. **Review-by-hand list: yes, per hunk**, for `verified_weak` and `unverified`, including
    removals (which per-line coverage cannot list). Should `verified` results with unexecuted
    hunks get it too? I'd say no for now: the verdict already says the evidence is enough.
11. **Scope**: whole-file mode only. Running the checks in agent mode and `apifix verify` (via
    the verifier) is a separate next step.
12. **Test files**: the unrelated-edit check applies to them; the config checks do not.

## Later: the checks in agent mode and `apifix verify` (decision 11; next step)

Run the unrelated-edit and dropped/ignored config checks in agent mode and in `apifix verify`
too, through the verifier (both are pure functions of the original tree, the fixed tree and the
findings): unrelated edits as a `warn` reason, dropped or ignored config as `reject`. This is the
next step after this one.

A `client_config` finding should count as resolved when the assignment is kept and the module's
calls use the module-level client (`openai.X(...)`). djgpt-gptoss-4 kept the CLI's
`openai.api_key` assignment and moved the call to the module-level client, as the suggestion
says, and the verifier still reported the finding as remaining.

## Amendments

all 12 decisions accepted as recommended

Decision 11: add running these checks in `apifix verify` (and agent mode) to the plan's
"Later" section as the next step (done above).

## Built

Items 1–4 with all 12 decisions as recommended; item 5 (agent mode, `apifix verify`) is the
next step (see "Later"). Changes from the plan as written:

- **Migration names:** besides names bound by a near code hunk, names such a hunk *introduces*
  (used on its candidate side, nowhere in the original, also as `self.x`) count. Without this, a
  `client` parameter threaded through a far signature for a call that now uses `self.client`
  was rejected. Names the original already uses (`os`, `text`) still do not count, so the
  inserted `_create_dummy_pdfs()` stays caught.
- **String rule exemptions:** also dict and subscript keys that became attribute or keyword
  names (`data["choices"]` -> `data.choices` in a function the scanner did not flag; `{"model":
  m}` -> `SimpleNamespace(model=m)`). Values stay protected. Strings in text-only hunks left
  after a revert that did not parse are not reported twice.
- **Code-error excerpts:** the first non-comment line of the hunk, with that line's number
  (the first non-blank line showed a `# ----` banner for the PDF insertion). An insertion at
  the top of a file is "before original line 1", which `error_key` also strips.
- **Revert records** carry `kind` (changed / added / removed) besides `line`, `fixed_line`,
  `old`, `new`; the report wording follows it ("text added outside the migration: ... removed",
  "blank lines added outside the migration, removed"). `fixed_line` is mapped through the
  imports apifix adds afterwards.
- **`check_candidate`** takes `findings` (the file's own; `None` = no unrelated-edit check) and
  `rewrite_checks`; the protocol hooks `adjust_findings` / `rewrite_checker` are looked up with
  `getattr`, so a migration without them still works.
- **Scanner wording:** for an `api_base` assignment in a file without the calls, the note is
  "The API calls are in X: keep the value set here (as openai.base_url)", not "keep this
  assignment". Locations are grouped per file ("set in voicebot/cli.py:12,13").
- **Check B message** names every value the new client misses, e.g. "does not read
  openai.api_key, which app/cli.py:9 sets from `key` or openai.base_url, which app/cli.py:10
  sets from `url` (as openai.api_base)".
- **`code_checks` mock suggestion** (item 2's "checked during the build"): it did *not* name a
  target when the module-level call's result is assigned (`r = openai.chat.completions.create(...)`):
  any library call was taken for a client constructor. Now only library classes
  (`openai.OpenAI`, capitalised last part) count as constructors, and the suggestion is
  `patch("app.openai.chat.completions.create") and set create.return_value`.
- **`diffing.opcodes`** is the one line diff behind `changed_lines`, `hunks` and `unrelated.py`.
- README: a short paragraph on the new whole-file checks and on "Review by hand".

### Real-diff check (re-run through the built modules, 2026-10-23)

Each run's `fix.diff` applied to a copy of the original repo (`~/apifix-targets/`), the
original scanned for findings, and each changed Python file run through `revert_text_edits`,
`unrelated_edits` and the openai-v1 `rewrite_checker`, as `check_candidate` does. resume-2's
and resume-gptoss-1's diffs have LF lines while the repo is CRLF, so both copies were converted to
LF first.

| Run | Reverted | Rejected (check retry) | Config | Legitimate hunks kept |
|---|---|---|---|---|
| djgpt-gptoss-3 | prompt.py:190 (written file: 193) `how such likely` | none | A: cli.py:69 removed `openai.api_key = openai_api_key`; B: prompt.py:132 `openai.OpenAI()` | all 13 |
| djgpt-gptoss-1 | prompt.py:190 (written file: 224), the same edit | none | A: cli.py:69; B: not flagged (the client comes through the `OpenAI = _OpenAI` shim, the documented miss) | all 15 |
| resume-2 | none | testapp.py: removed `if __name__ == '__main__':` block (reported at original line 120, its first code line; hunk 118–121) | none | all 8 |
| resume-gptoss-1 | app.py: a blank line inserted at 59, inside the refactored 54–70 region | app.py: `def _create_dummy_pdfs():` added after line 21; the base64/PDF refactor at original lines 54, 57, 69; testapp.py:109 string `'Some reply from the mocked API.'` missing | none | all 13 others |
| resume-gptoss-1-verify | none | testapp.py:109, the same string | none | all 13 |
| resume-gptoss-2 | none | none | none | all 13 |

This is what the prototype caught. The prototype table gave resume-gptoss-1 "all 14 others"; the
built check counts 18 hunks there, of which the blank-line revert is inside the refactored
region the prototype listed as one item, so 13 remain. No resume run is flagged by check A: the
removed `openai.api_key = os.environ.get("OPENAI_API_KEY")` is an env read 1.x does itself.
resume-gptoss-1's attempt 1 (`attempt1-crlf.diff`, not in the prototype's set) gets only the same
testapp.py:109 string error.

### Tests

- `tests/fixtures/split_config/` (synthetic): `voicebot/cli.py` sets `openai.api_key` and
  `openai.api_base` from parameters, `voicebot/assistant.py` has a system prompt with trailing
  spaces and a double space plus the 0.x call, `tests/test_assistant.py` mocks
  `openai.ChatCompletion.create` with a dict (including a value no test reads), asserts a mock
  is not called and has a `__main__` block.
- `tests/test_unrelated.py`: reverts (prompt word with both line numbers, trailing spaces,
  docstring, far comments reworded / removed / inserted, whitespace), kept changes (client
  creation, imports, near comments, threaded client parameter, `get_client()`, missed response
  access, call reflow, mock renames near and far), rejections (`__main__` block, refactor,
  inserted file-writing helper, `model=` string, mock content, unused mock value), one message
  per rejected hunk, a revert that does not parse, unparsable files, CRLF, `error_key`.
- `tests/test_openai_config.py`: check A (parameter, kept, passed to `OpenAI(...)`, through a
  variable, env reads 1.x does / does not do, literals, `None`, `api_base` kept / renamed /
  dropped, alias, test files) and check B (new `OpenAI()`, the fixture's message, passing the
  values, `**kwargs`, the module-level client, no configured value, test files).
- `tests/test_scanner.py`: statement-level config findings per value class, value in the
  suggestion, reads, Azure, the repo pass (fixture, async, env-only key, test-file key).
- `tests/test_migrations.py` (client.config selection and "(see below)"; the failure hint),
  `tests/test_code_checks.py` (assigned module-level call), `tests/test_whole_file.py` (revert
  before auto-imports, line mapping, config last, one error per problem), `tests/test_agent.py`
  (whole-file replay on the fixture: B and A through check retry, the prompt word reverted,
  report lines, verified_weak with the review list), `tests/test_verifier.py` (review list:
  removal, comment, string edit, executed hunk left out, cap, verdicts, line forms),
  `tests/test_diffing.py` (hunks).
- Docker: `tests/integration/test_split_config_docker.py`: the fixture's good migration
  (assignment kept, `api_base` -> `base_url`, module-level client, tests that set
  `openai.api_key = "test"` in `setUp` and patch `openai.chat.completions.create`) passes all
  checks on the first reply and its 3 tests pass in the sandbox on real openai 1.x, smoke check
  included.
