# Region edits for large files (whole-file mode)

## Request

Context: Read CLAUDE.md. Whole-file mode can’t handle larger files on free tiers: on rgbkrk/genai (~/apifix-targets/genai, run at ~/apifix-runs/genai-gptoss-1/) the check retry for tests/test_context.py needed ~9,700 tokens (prompt plus room for the whole rewritten file) against the hosted provider’s 8,000 TPM limit, and the job aborted. stoyan-stoyanov/llmflows (72 findings) will be worse.

Task: record this request verbatim in docs/plans/region-edits.md, write the plan, and stop for my review. Don’t write code yet.

Region edits for large files: when a file is over a threshold (or the request wouldn’t fit the known per-minute or context limit), send the model only the regions that need changing: each top-level function, method or class body that contains a finding, plus the file’s import block and the module-level names those regions use, as read-only context. The model returns each region in its own fenced block, labelled; apifix splices them back by position. Say how regions are identified (AST spans), labelled and validated (each block must parse in its place, the spliced file must parse, imports may be added through the import block), and how all the existing checks (unrelated edits, mock targets, undefined names, config checks, auto-imports) work on the spliced file.
Choice of mode: automatic per file (whole-file for small files, regions for large ones), with a flag to force either. Say what the threshold should be.
Retries: a check retry for a region-edited file resends only the affected regions with the error.
The trimming steps deferred in docs/plans/deterministic-pins.md (item 7, steps 2–6): say which are still worth doing once region edits exist.
Estimate from genai’s trajectory how big the requests would have been in region mode.

List decisions for me at the end.

## Findings

From `~/apifix-runs/genai-gptoss-1/trajectory.json` (`openai/gpt-oss-120b`, 8,000 TPM), the
other whole-file runs in `~/apifix-runs/`, and `apifix scan` of genai and llmflows. Prompt
tokens are the server's `input_tokens + cache_read_tokens`. In this run, the request texts
come to 3.8 characters per prompt token (20,068 characters of `prompt_parts` for 5,290 tokens;
22,540 for 5,890). The estimates below use that ratio.

### What happened to tests/test_context.py

| Request | Prompt | Reply used | max_tokens | Result |
|---|---|---|---|---|
| Attempt 1 | 5,290 | 3,834 | 6,000 | mock target `genai.context.OpenAI` rejected |
| Attempt 1, check retry | 5,890 | 3,731 | 6,000 | same error again: attempt ends |
| Attempt 2 (same form as the check retry) | ~5,900 | – | 6,000, then 4,123 | 413 twice, job aborted |

The prompt was rules 1,309 + guide 4,622 + file 10,141 + context (genai/generate.py's working
copy) 2,726 + findings and instructions 1,270 characters, plus the 2,472-character previous
version note on the retries. gpt-oss needs about the file's own tokens (10,141 / 3.7 ≈ 2,740)
plus ~1,000 of reasoning in its reply. 5,900 + 3,800 ≈ 9,700, so no `max_tokens` could have
made the attempt-2 request fit. (the hosted provider's own counts are not consistent: the first 413 implies a
3,621-token prompt, the second one 5,601. The usage figures above are the reliable ones.)

All three findings (lines 153, 154, 177) are in a single function,
`test_build_context_assistance`, lines 152–213: 1,629 of the file's 10,141 characters (16%).

### Every whole-file request so far

On the hosted provider's 8,000 TPM, every Python file up to 8 KB fit as a whole file: djgpt `prompt.py` at
7,980 bytes (prompts 3,506–6,100, replies 2,137–3,828) and wolverine `wolverine.py` at 7,797.
suspense `misc.py` (9,541 bytes, no context) also fit, at up to 4,673 + 2,915. test_context.py
did not, at 10,144 bytes plus 2,726 of context. What decides is the whole request
(file + context + feedback + previous version + reply), not the file size alone.

### Region sizes (scanner findings, regions as defined in item 1)

| File | Bytes | Regions | Region bytes | Import block | Outline |
|---|---|---|---|---|---|
| genai `tests/test_context.py` | 10,141 | 1 | 1,629 (16%) | 254 | ~860 |
| genai `tests/test_suggestions.py` | 10,286 | 4 | 7,561 (73%) | 200 | ~730 |
| genai `tests/test_magics.py` | 3,160 | 3 | 3,051 (96%) | 103 | ~160 |
| genai `genai/generate.py` | 2,563 | 2 | 1,773 (69%) | 110 | ~220 |
| llmflows `llms/azure_openai_chat.py` (largest of 5 similar) | 6,063 | 3 methods | 3,341 (55%) | 322 | ~240 |
| llmflows `llms/openai_embeddings.py` | 4,206 | 3 methods | 2,988 (71%) | 350 | ~140 |
| llmflows `tests/llms/test_openai.py` | 2,935 | 1 method | 1,052 (35%) | 145 | ~340 |

### llmflows is a context problem, not a file-size problem

llmflows' 72 findings sit in five non-test modules of 4.2–6.1 KB (12–15 findings each) and
three test files under 3 KB. No file is over any sensible threshold. Two things make its
requests large:

- **The guide.** Its findings select 10 guide parts (client, async, azure, kwargs, config,
  resources, responses, errors, tests, dependencies): about 7,700 characters without
  Dependencies, so the system prompt is ~9,000 characters (~2,370 tokens) on every request,
  versus 5,931 for genai.
- **Read-only context for test files.** Each test file gets every changed non-test Python file,
  up to the 24,000-byte budget: about four of the five modules, ~20 KB (~5,300 tokens). A
  request for `tests/llms/test_openai.py` would be ~8,600 prompt tokens before any reply, over
  8,000 on its own. Region mode on the 2.9 KB test file alone saves ~370 tokens. That does not
  help.

Restricting context to modules the test imports is not safe. test_context.py reaches
`genai/generate.py` (the module whose patch target it got wrong) only through IPython's
`%%assist` magic, not through any import (`genai.context` imports only `genai.display`).
Item 6 handles context differently.

## Plan

### 1. Regions (`engine/regions.py`, new; pure `ast` + stdlib)

`find_regions(path, original, findings) -> RegionPlan`, computed on the **original** file. Each
`Region` has an id, kind, name, line span (whole lines; 1-based, inclusive), indentation and
the findings it holds.

- **Statements:** for every finding line, take the top-level statement that holds it. Its span
  runs from its first decorator to `end_lineno`. A `def` / `async def` / `class` statement is
  one region, with nested functions included. Other compound statements (`if __name__ ==
  "__main__":`, a module-level `try`, `with` or `for`) are also one region each, and so is a
  module-level simple statement (`openai.api_key = ...`).
- **Classes are split into methods:** a finding inside a method gives the method (decorators
  included) as the region, labelled `Class.method`. A finding in a class-level statement gives
  that statement. A finding in the class header (bases, decorators, keywords) gives the lines
  from the first decorator up to the line before the body. Only one level: a nested class's
  method gives the nested class's whole statement.
- **Import block** (`IMPORTS`, always offered, editable): the leading run of top-level
  statements after the module docstring and `from __future__` imports. Each statement in it is
  an import, a `try` whose body contains only imports (handlers may assign fallbacks), or an
  `if TYPE_CHECKING:` that contains only imports. Comments and blank lines between them count.
  A file without one gets an empty block at the insertion point `add_missing_imports` already
  uses: after the docstring and `__future__` imports. A finding inside the block (`from
  openai.error import RateLimitError`) is handled there, and needs no other region.
- Imports further down the file are not part of the block. A finding on such an import gives
  its statement as a region.
- Comments directly above a `def` belong to the gap, not to the region, so they cannot change.
  Regions never overlap (all are top-level statements or members of one class body), and
  adjacent ones stay separate.

**Read-only context** inside the file, built from the original:

- **Names used:** names loaded in a region (`ast.Name`). Left out: builtins, names bound inside
  the region, and names the import block binds (the block is shown anyway). Each remaining
  name with a module-level definition outside the regions is shown with its line. A
  def/class is shown as its decorators and signature line(s) followed by
  `# line N, body not shown`. No `...` stubs, so the model is not shown a placeholder form to
  copy. An assignment of up to 3 lines is shown whole, a longer one as its first line plus
  `# N more lines`. For method regions: the class header with class-level assignments, and
  the signature lines of other methods the regions call through `self.` / `cls.`. Capped at
  2,000 characters: whole definitions first, then signature-only, then names only.
- **Outline:** one line per top-level def/class (and per method), with its line range, and
  regions marked. Capped at 1,000 characters (names only beyond that). It tells the model what
  exists around the regions; the mock-target messages already list top-level names this way.

### 2. Request and labels

The request keeps the whole-file layout (feedback first, so the prefix can be cached; then
findings), with the file's contents replaced by:

```
Edit only these regions of tests/test_context.py (the rest of the file is unchanged and not shown).

Imports (IMPORTS, original lines 1-14; return it only if you change it):
```python IMPORTS
...
```

Region R1: def test_build_context_assistance (original lines 152-213)
```python R1
...
```

Defined elsewhere in this file (read-only): ...
Outline (read-only): ...
```

Regions are shown verbatim, with their original indentation, and numbered `R1..Rn` in source
order. The instruction: "Return every region R1..Rn, each complete from its first line
(decorators included) to its last, in its own fenced code block with its label after the
language (```` ```python R1 ````). Return IMPORTS only if you change it. You may add imports,
and module-level assignments the migration needs (`client = OpenAI()`), at the end of
IMPORTS. Do not return the rest of the file." For a region with only [likely] findings, the
same "return it unchanged if it needs no change" applies as for whole files.

The system prompt is fixed per job (caching). Its "Reply with the complete updated file ..."
rule becomes: "Reply in the form the request asks for: the complete file in one fenced code
block, or each requested region in its own labelled block. Never abbreviate ...". This applies
to every whole-file-mode job.

**Parsing** (`parse_region_blocks`, next to `parse_code_block`, same fence rules): every
complete fenced block, with its label taken from:

1. the info string: `python R1`, `py R1`, `R1`, `python IMPORTS` (case-insensitive);
2. otherwise the last non-blank line before the block, if it holds exactly one label
   (`### R1`, `**Region R1**`, `R1:`).

Unlabelled blocks are accepted by position only when every block is unlabelled and their
number equals the number of required regions (or that number plus one, the first block being
IMPORTS when it consists only of imports and assignments). The trajectory records
`labels: "by_order"`. Anything else is a format error, and gets one nudge in the existing
conversation (counted with the code-block nudges): "Return each region in its own code block
labelled with its id: R1, R2 (and IMPORTS if changed)." The nudge names the labels that were
missing, duplicated or unknown. A truncated reply is not nudged, as today.

**A whole file instead:** a reply that is one unlabelled block, parses as a module, and
contains every top-level name of the original is treated as a whole-file candidate. It then
goes through the whole-file checks unchanged (recorded as `form_returned: "whole"`). This costs
nothing to accept, and small models will do it.

### 3. Validation and splicing

Per block, before splicing:

- **Indentation:** a method block that comes back dedented to column 0 is re-indented by the
  region's indentation. Indentation that is neither the original nor 0 is an error.
- **It parses in its place:** the dedented block must `ast.parse`. A def/class region must
  still be exactly one statement of the same kind and name (a method may not become a module
  function). A statement region (an assignment, an `if __name__` block) may become any number
  of statements, since `openai.api_key = x` can legitimately turn into a client assignment.
  IMPORTS may hold only what item 1 allows in the block, plus simple or annotated assignments
  after the last import. Errors name the region:
  "R1: SyntaxError: ... (line 7 of your block)".
- **Abbreviation:** the placeholder check per block, against that region's original lines.
  The length check (fewer than half the lines when the original has 20 or more) also runs per
  region.

**Splice** (`splice(original, plan, blocks) -> Spliced`): replace each region's original lines
with its block, from the bottom of the file up so that earlier spans stay valid. Line endings
and the final newline are those of the original (`as_file`). Regions not returned (only
IMPORTS may be left out) keep their original text. `Spliced` keeps a line map: for every line
of the spliced file, its region and its line within the block, or the original line it was
copied from.

The spliced file must parse. If a block parses on its own but breaks the file (e.g. a method
whose indentation is consistent but wrong for its class), that is an error on that region.

### 4. Existing checks on the spliced file

`check_candidate(path, original, spliced, ...)` runs as it is now, with the same arguments. The
spliced file is a complete candidate, so no check needs a region-aware variant:

- **Unrelated edits** (`unrelated.py`): the diff is original vs spliced. Hunks can only fall
  inside regions or IMPORTS. The rules are unchanged: text-only hunks are reverted, code hunks
  are kept when near a finding's statement or openai-bound, and missing strings are errors.
  Reverted hunks are inside regions; `relocate` already gives their final line numbers.
- **Auto-imports:** `add_missing_imports` inserts after the docstring and the leading imports,
  which is the end of IMPORTS. After it runs, the line map is shifted by the inserted lines
  (the same step `relocate` does for reverts).
- **Undefined names** (pyflakes): run on the whole spliced file, so names defined outside the
  regions are seen. A name that a region uses and that the model deleted from outside cannot
  occur, since the outside never changes.
- **Mock targets, code under test, library targets:** they compare the original and the
  spliced test file, and read other modules from the working copy, as now.
- **Config checks:** the A/B rules run on the whole spliced file. A dropped `openai.api_key`
  assignment is caught wherever it was. A new client constructor in a region must pass the
  configured keywords.
- **"Changed" / dependency checks:** "changed" applies file-wide, as now. Dependency files are
  never region-edited (item 5).

The written file is the checked text (`result.text`), as now.

### 5. Choice of form, per file (`choose_form`)

New config `file_edits: "auto" | "whole" | "regions"` (CLI `--file-edits`, whole-file mode
only; default `auto`) and `region_threshold_bytes` (`--region-threshold BYTES`, default
**8,000**). Dependency files and non-Python files are always whole. For Python files under
`auto`, in order:

1. **Regions** when the file is over the threshold.
2. **Regions** when a limit is known (item 7) and the whole-file request estimate (prompt plus
   the reply estimate `file_tokens`) does not fit it.
3. **Whole** when the regions plus IMPORTS cover 80% or more of the file's bytes (test_magics.py:
   96%). Sending everything is then simpler and safer, and the saving is small. This applies
   only when the whole-file form still fits under 2.
4. **Whole** otherwise.
5. **Switch on refusal:** when a whole-file request gets a `RequestTooLarge` that lowering
   `max_tokens` cannot fix (today an abort), the file switches to regions and the request is
   sent once in that form. The switch is recorded (`form_reason: "refused"`).

A file stays in the form it was first sent in for the rest of the job: whole → regions only
by rules 2 and 5, never back. `--file-edits whole` keeps today's behaviour exactly (rule 5
included). `--file-edits regions` region-edits every Python file with findings.

**Why 8,000 bytes:** 8 KB of Python is ~2,100 tokens, which appears twice in a whole-file
request (prompt and reply): ~4,300 tokens, about half of the hosted provider's 8,000 free-tier limit. The rest
is left for the system prompt (1,500–2,400) and the feedback. All Python files up to 8 KB fit
on that limit in every run so far (largest 7,980), and the one that failed was 10.1 KB. Below
the threshold, whole files are the better form: the model can also fix helpers and mock data
outside the findings' functions. Above it, regions also help models with large limits: fewer
reply tokens to get right, fewer truncated replies, less code the unrelated-edit check has to
judge (suspense misc.py, 9.5 KB, failed attempt 2 on an unrelated edit at line 103).

**`whole_file_max_bytes` (24,000):** today a file over it is skipped. With regions, the limit
applies to what is sent (regions + IMPORTS + read-only parts) in one request, not to the file.
A 60 KB module with one 2 KB function to change becomes sendable. A file is skipped only when a
single region is over the limit (reason: "region R2 (def huge, 31,000 bytes) is over the size
limit").

**Batches:** when the regions do not fit one request (over `whole_file_max_bytes`, or over a
known limit by the item 7 estimate), they are sent in several requests ("batches"), in source
order, packed greedily. Each batch gets IMPORTS as it is after the batches before it (editable
again, last version wins), only its own regions' findings, and the earlier batches' accepted
regions as a read-only diff (capped at 1,500 characters). Every batch is checked on the file
spliced so far, with `required=False` until the last batch, so its check retry stays within the
batch. The last batch runs the full checks. genai test_suggestions.py needs two batches on
8,000 TPM (see Estimates).

### 6. Read-only context from other files (test files only)

The whole-file rule stays: changed non-test Python files are context, and none are dropped
for relevance (test_context.py needed generate.py). The form changes:

- **Region-edited test files, and any test file once a limit is known:** each changed module is
  shown as its changed top-level statements only (the fixed text of each statement whose text
  differs from the original, with its line and its enclosing class), plus its import lines
  that mention the library. Then come the modules the test file imports (transitively, through
  repo modules and one level of package re-exports, `from llmflows.llms import OpenAI` →
  `llms/openai.py`), first, then the rest. When that is over the budget, the modules the test
  does not import shrink to their *library uses*: the statements whose AST has a name bound to
  the library (the `api_modules` imports, as in `code_checks`' suggestions), at most 600
  characters per module. They are the facts a test needs for mock targets: how the module
  binds `OpenAI` and which call chain it uses.
- **Small test files with no known limit:** full working copies, as today.

`context_left_out` and `prompt_parts.context` stay. A new `context_forms` records
`{path: "full" | "changed" | "library_uses"}` for each context file.

### 7. Knowing the limit

Needed by rules 2 and 5 and by batching. This part comes from the deferred plan's "Knowing the
limit".

- A per-minute limit stated by a `RequestTooLarge` is kept for the rest of the job (today
  `_fitting_max_tokens` forgets it). Ollama's runtime context window (`context_window`) also
  counts as a limit. When both are known, the smaller one applies.
- Estimate of a request: characters / ratio + `max_tokens`, where the ratio is the lowest
  `prompt chars / (input_tokens + cache_read_tokens)` seen in this job (most tokens per
  character; 3.8 here). Before any response, the ratio is 3.0, on the safe side for Python and
  English. A request counts as fitting when its estimate plus `token_limit_margin` is at most
  the limit.
- `--max-output-tokens` also counts: a whole-file reply estimate over it would be truncated, so
  rule 2 picks regions.

### 8. Retries

**Check retry (same attempt):** the errors of the spliced candidate are attributed to regions
via the line map (`attribute_errors`):

- `original line N`, `after/before original line N`: the region (or IMPORTS) holding original
  line N, or the nearest region above it.
- `line N` (candidate lines: undefined names, config B, syntax): the region the line map gives.
  The message is rewritten to "R1, line 12 of your block: ..." so the model can find it.
- Errors with no line (mock targets, library targets): the region whose block contains the
  quoted target string. Otherwise, and for file-wide errors ("changed", config A when the
  statement's region is unknown), all regions.

The retry is a fresh request, like today's (the shared prefix stays). It contains: the affected
regions' **original** text (editable), each with its rejected version as a diff against its
original (`_previous_version`, per region), and the error(s); IMPORTS as the model last
returned it (editable); and the other regions' accepted changes as a read-only diff (capped
at 1,500 characters), so a fix in R2 can rely on a name R1 introduced. The candidate checked
after the retry is the original with the kept regions from the previous reply plus the new
blocks. "Keep these fixed" works as now, per file. One check retry per file (per batch with
batches), as today.

**Later attempt (after a failed verification):** all of the file's regions again, from the
original, with the previous version as per-region diffs, like whole files. Regions are also
**added** from the verifier's output for this file: a failing or erroring test of this file
(test id → its function or `Class.method`), and every traceback frame `path:line` in this file
(the top-level statement or method holding the line). These are the edits whole-file mode
could make (mock data in a helper, a fixture) that the findings did not point at. They are
recorded as `regions[].source: "feedback"`. Region ids stay stable within the job: new ones
continue the numbering, and the labels stay in source order within each request.

### 9. Trajectory and report

- Each file entry: `form` (`whole` | `regions`), `form_reason` (`size`, `limit`, `refused`,
  `coverage`, `forced`, `small`). With regions: `regions: [{id, kind, name, lines, bytes,
  findings, source, returned, changed}]` and `batch`. `labels` (`labelled` | `by_order`).
  `prompt_parts` gains `regions`, `imports`, `names`, `outline` (in place of `file`). The
  `bytes_sent` value is what was actually sent.
- Check-retry nudges: `regions_resent` and `attributed` (error → region).
- Job: `limits` (`per_minute`, `context_window`, `chars_per_token`) when known.
- Report: one line per region-edited file, e.g.
  `- Region edits: tests/test_context.py (10,144 bytes, over 8,000): R1 test_build_context_assistance (lines 152-213); 16% of the file sent`,
  and the file's failure lines name the region (`tests/test_context.py R1 failed: ...`).

### 10. What region mode cannot do (known limitations to document)

- Edits outside the regions and IMPORTS: helpers, fixtures and mock-data builders without
  findings, and comments above a def. Later attempts add regions from failing output (item 8),
  but a helper that no failure names stays out of reach. Same-file functions that return dict
  literals and that a region calls (the shape `code_checks` already names) are shown as
  read-only names, not editable.
- New module-level code only at the end of IMPORTS, and only assignments: no new functions or
  classes at module level. A new method needs its class as the region (decorators/header
  finding) or a method region of the same class (the block must stay one def; see decision 4).
- One region is at least one whole top-level statement or method. A 600-line function with one
  finding is sent whole, and if it is over the size limit the file is skipped.
- Labels and splicing depend on the model returning complete blocks. A block that silently
  drops a nested function passes parsing; the unrelated-edit check (missing strings, removed
  code) is what catches it.

### Agent mode and `apifix verify`

Unchanged. Agent mode already edits by exact string replacement and reads files in pieces.
`apifix verify` has no model.

## The deferred trimming steps (deterministic-pins.md item 7, steps 2–6)

| Step | Still worth it? | Why |
|---|---|---|
| 2. Only the guide parts this file needs | **Yes**, on a known limit | The guide is the largest fixed part and regions do not touch it: 4,622 characters for genai, ~7,700 for llmflows (~2,000 tokens, the biggest single item in a llmflows region request). Dropping Tests (~2,450 characters) for non-test files is what lets llmflows' attempt-2 requests fit (Estimates). |
| 3. Less test output in the feedback (3 × 800, 2,400 total) | **Yes**, on a known limit | Feedback at its cap (~2,100 tokens) is the largest variable part from attempt 2 on, in both forms, and it is the same for every file. Without it, genai test_context.py's attempt-2 region request only fits with `max_tokens` lowered to ~1,560, which its reply (~1,150–1,550) barely fits. |
| 4. Context as diffs, then dropped | **Replaced by item 6** | Diffs of whole modules are still large for llmflows (5 modules). Changed statements, imported modules first, then library uses, keep what tests need. Dropping context loses exactly the module the test must patch (genai). |
| 5. Previous diff capped at 1,200 | **No** | In region mode the previous version is per region and only for the regions resent. Whole files are now at most 8 KB, so their 3,000-character cap is rarely reached. |
| 6. Lower `max_tokens` to fit | **Already built** | Stays as it is; rule 5 adds the switch to regions when lowering cannot help. |
| Remembered limit + calibrated pre-check | **Yes, now required** | Rules 2 and 5 and batching need it (item 7). |

Steps 2 and 3 apply only once a limit is known, as the deferred plan proposed, so local models
with no stated limit keep identical prompts across a job.

## Estimates

Tokens = characters / 3.8. Reply needed = code tokens (characters / 3.7) + ~700–1,100 of
gpt-oss reasoning, as measured in this run. Reply budget = `reply_budget` (2 × ⌈chars/3⌉ +
2,048). "Requested" = prompt + budget, which is what the hosted provider checks. Region request parts:
system 5,931 (unchanged), findings 738 + instructions ~880, IMPORTS 254, outline ~860, names
0 (the region uses `build_context`, imported, and the `ip` fixture), R1 1,629, context
generate.py 2,726 in full, or ~600 as library uses (item 6).

### genai tests/test_context.py

| Request | Whole file (measured) | Regions, context full | Regions, context compact |
|---|---|---|---|
| Attempt 1: prompt | 5,290 | ~3,430 | ~2,870 |
| Reply needed / budget | ~3,800 / 6,000 | ~1,150–1,550 / 3,134 | same |
| Requested | 11,290 (got through) | **~6,560** | ~6,000 |
| Check retry: prompt (+ note ~2,470) | 5,890 | ~4,080 | ~3,520 |
| Requested | ≥ 9,700 needed | **~7,210 ≤ 8,000** | ~6,650 |
| After a verifier rejection, feedback at its cap: prompt | ~8,000 | ~6,180 (fits only with `max_tokens` ~1,560) | ~5,620 |
| … with step 3 | – | ~4,710 + 3,134 = 7,840 | ~7,280 |

Rule 1 (10,144 > 8,000) chooses regions on the first request, so no limit has to be learnt
first. The aborted attempt-2 request (same form as the check retry) would have been ~7,200
tokens instead of ~9,700, and the reply would have held 16% of the file (R1) instead of all
of it.

### genai tests/test_suggestions.py (never reached in the run)

Whole file: ~5,640 prompt + ~3,800 reply ≈ 9,400: does not fit. One region request (4 regions,
7,561 bytes): ~5,260 prompt, reply estimate 2,521, and lowering `max_tokens` leaves 2,484:
does not fit either. Two batches (R1+R2 3,999 bytes, R3+R4 3,562): ~4,000 prompt each (~3,440
compact), budget 4,714 lowered to ~3,700, reply needed ~2,100: **fit**.

### llmflows (no run yet; system prompt ~9,000 characters)

| File, request | Today | With this plan |
|---|---|---|
| `llms/azure_openai_chat.py`, attempt 1 (6,063 bytes, whole) | ~4,650 + reply ~2,640: fits after lowering | unchanged (under the threshold, fits) |
| same, attempt 2 (feedback + previous diff) | ~7,540 prompt: **no room** | regions (rule 2) + steps 2 and 3: ~4,680 prompt, reply ~1,900: fits |
| `tests/llms/test_openai.py`, attempt 1 | ~8,600 prompt (≈4 modules of context): **no room** | context compact (item 6): ~4,520 + budget 2,750: fits |

For llmflows, item 6 and steps 2–3 carry most of the gain. Region mode for its modules saves
~40% of each file (55–71% coverage) and makes the retries fit.

## Tests (synthetic fixtures only)

- `test_regions`: region finding (functions with decorators, async, nested functions, methods,
  class-level statements, class headers, `if __name__`, module `try`, module assignment,
  findings in the import block and in a later import, files without imports, docstring and
  `__future__`, CRLF, no final newline). Names-used and outline building, with the caps.
  Splicing bottom-up, re-indenting, line endings, the line map.
- Parsing: info-string labels, heading labels, unlabelled by order (and the IMPORTS-first
  case), duplicate / unknown / missing labels, a whole file returned, truncation.
- Validation: same kind and name, a statement region turned into several, IMPORTS
  restrictions, syntax in a block vs only after splicing, per-region placeholder and length
  checks.
- `check_candidate` on spliced files: one case per check (a reverted text hunk inside a region,
  an unrelated code hunk, auto-import landing in IMPORTS with the line map shifted, an
  undefined name, a bad mock target, a dropped `openai.api_key` in a statement region).
- Error attribution, one per message form, and the "R1, line 12 of your block" rewrite.
- `test_agent` with a scripted provider: rule-by-rule form choice (size, coverage, known
  limit, refusal switch, `--file-edits` forced either way); a check retry resending only the
  affected region with the other regions' diff; batches (IMPORTS carried over, per-batch
  retry, full checks on the last); a later attempt adding regions from a failing test and a
  traceback line; a whole file returned to a region request. A genai-shaped repo (10 KB test
  file, one function with three findings) against a fake provider that refuses requests over
  8,000 estimated tokens runs to verification.
- Remembered limit and ratio calibration; context forms (changed statements, imported first
  through a re-export, library uses).
- `test_cli`: the flags, and report and trajectory keys.
- Docker: one synthetic repo with a module over 8 KB, region-edited by a scripted provider,
  verified end to end.

## Docs

CLAUDE.md: layout line for `regions.py`; `--file-edits` / `--region-threshold` in the CLI
line; a "Known limitations (region edits)" section (item 10, labels by order, batching, the
80% rule, the remembered limit and its estimate); whole-file's size-limit paragraph updated
(regions instead of skipping). README: a paragraph on region edits for large files and free
tiers.

## Afterwards

Rerun genai on a hosted endpoint (same model) and compare with genai-gptoss-1, then run llmflows.

## Decisions for you

1. **Threshold and auto rules:** 8,000 bytes, plus "does not fit a known limit", plus the
   80%-coverage fallback to whole files, plus the switch on a refusal (my proposal). A lower
   threshold (e.g. 6,000) gives more headroom on 8k-TPM tiers but takes the whole-file form
   away from files that fit today (prompt.py, wolverine.py).
2. **Flags:** `--file-edits auto|whole|regions` and `--region-threshold BYTES` (my proposal),
   or one flag (`--region-threshold 0` forces regions, a huge value forces whole files).
3. **Class granularity:** always methods (my proposal), or a whole class when it is small
   (e.g. under 1,500 bytes), which lets the model add a method.
4. **What IMPORTS may gain:** imports plus module-level assignments at its end (`client =
   OpenAI()`; my proposal), imports only, or also new module-level functions (a client
   factory).
5. **Region blocks required:** every region must come back (my proposal; like "return it
   unchanged"), or a missing block means "unchanged" (fewer reply tokens, but an
   accidental omission is not caught until the verifier).
6. **Unlabelled blocks:** accept by position when the count matches (my proposal), or always
   nudge for labels.
7. **Batches** when a file's regions do not fit one request (my proposal), or skip the file
   with a reason.
8. **Regions added from verifier output** on later attempts (failing tests and traceback lines
   in the file; my proposal), or findings' regions only.
9. **Context rework (item 6)** in this step (my proposal; it is what llmflows needs), or
   deferred to its own step.
10. **Deferred steps 2 and 3:** build them now, active only on a known limit (my proposal), or
    leave them deferred and build region edits alone. Steps 4–5 are not built, 6 exists.

## Amendments

Approved. Record in the plan’s Amendments section, verbatim: “Decisions 1–3 and 5–10 as proposed. Decision 4: the import block may gain imports, module-level assignments and new module-level functions (e.g. a get_client() factory, as in the verified wolverine fix); everything added is judged by the existing checks, including unrelated edits.”

Build in stages and commit after each one (the first commit includes the plan file), with the fast suite and ruff passing (Docker on the last stage, network permitting), so an interruption loses at most one stage:

Region finding, labelling, parsing, validation and splicing (regions.py) with its tests.
Whole-file integration: automatic form choice, the flags, check retries per region, batches, all existing checks on the spliced file, report and trajectory.
The context rework (item 6), steps 2 and 3, the remembered limit, and regions added from verifier output.

Synthetic fixtures only. Don’t run live tests. After stage 3, record under “Built” what changed from the plan.
