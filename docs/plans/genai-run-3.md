# genai run 3: stream mock chunks, retries under a known limit, attempt budget, network blips

## Request

Context: Read CLAUDE.md and docs/plans/region-edits.md. genai run ~/apifix-runs/genai-gptoss-3/ (~/apifix-targets/genai) passed 4 of its 5 files’ checks but aborted without verifying. Record this request verbatim in a new plan file, docs/plans/genai-run-3.md, then build it.

Streaming mock chunks: in tests/test_generate.py, the model converted mocked stream chunks (yield {"choices": [{"delta": ...}]}) to objects, and the unrelated-edit check rejected that twice (original lines 8 and 17). The scanner’s mock-dict rule only knows "message" / "text". Extend it to "delta" chunks (a [likely] response_access finding), so converting them counts as migration work. Replay this run’s attempt 1 reply for that file: it should be accepted.
Retries must respect the known limit: tests/test_suggestions.py was sent as 4 regions; after a check failure the retry resent all 4 at once (9,425 tokens against 8,000) and the job aborted. Check retries (and later attempts) of a region-edited file must batch, or resend only the regions the error points at, under the known limit, the same way first requests do. Replay to confirm the retry would fit.
Attempt budget: each attempt here ended at the first file that failed a check, so 5 files used up the 4 attempts before any verification. Propose the simplest safe change and build it: for example, a file that fails its check retry is set aside for the rest of the attempt while the other files continue, and the attempt counts toward the limit only once all files have been tried; or check failures get their own per-file budget separate from verification attempts. Explain the choice in the plan.
Network blips: when the endpoint is unreachable mid-job (DNS failure, connection refused, reset), wait and retry with backoff (e.g. 30 s, 60 s, 120 s) before aborting, and record the waits in the notes. In genai-gptoss-3’s first try, a brief outage aborted the run.

Synthetic fixtures only. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

From `~/apifix-runs/genai-gptoss-3/trajectory.json` (`openai/gpt-oss-120b` on a hosted endpoint, 8,000 TPM):

| Attempt | Files sent | Result |
|---|---|---|
| 1 | generate.py ok, test_context.py (regions) ok, test_generate.py failed twice | check_failed |
| 2 | test_generate.py failed twice (same two errors) | check_failed |
| 3 | test_generate.py ok, test_magics.py ok, test_suggestions.py (4 regions, 1 batch) failed twice | check_failed |
| 4 | test_suggestions.py: 413 (requested 9,425), lowered to 4,319 and refused again | aborted |

- **test_generate.py:** its only finding was the mock target on line 32. `mock_deltas()` yields
  two `{"choices": [{"delta": {...}}]}` dicts; the scanner knew only `"message"` / `"text"`
  choices, so their conversion to `SimpleNamespace` had no finding nearby and was "changed
  code unrelated to the migration". The model was right both times.
- **test_suggestions.py:** no limit was known in attempt 3, so its 4 regions (7,561 bytes) went
  as one batch; the check retry went through. Attempt 4's request (all 4 regions again, each
  with its previous diff, 4,105 characters) was the first one the hosted provider refused. The limit was then
  known, but a region request had no way to use it: lowering `max_tokens` could not help, and
  the job stopped. `_batches` also never counted the previous-version note or the context.

## Plan

### 1. Stream chunks in mock data (scanner, openai-v1)

`_is_response_dict` also accepts a choice dict with a `"delta"` key (`rules.RESPONSE_CHOICE_KEYS`
gains it). Such a chunk gets its own suggestion, `rules.MOCK_DICT_CHUNK`: "1.x stream chunks are
objects: build mock chunks with attribute access (e.g. SimpleNamespace(choices=[SimpleNamespace(
delta=SimpleNamespace(content=...))])), not dicts." It is a [likely] `response_access` finding,
test files only, as before. Its text has "delta", so it also selects the Streaming guide part
for the file. The unrelated-edit check needs no change: the finding's region is the `yield`
statement, and the dict keys that became attribute names are exempt from the string check.

### 2. Region requests under a known limit (check retries and later attempts)

Every request of a region-edited file is checked against the known limit before it is sent,
not only the first batch's (`_batches`, which stays for planning the first requests):

- **Split before sending:** a request whose estimate (prompt characters / the job's ratio plus
  the reply estimate, plus the margin) does not fit the known limit, and that has more than one
  region, is split into groups packed greedily in file order. Each group's estimate is the
  request actually built for it (its own previous-version diffs and findings, the smallest
  context option), so the previous note and the context count, unlike `_batches`' formula.
  This applies to a batch's first request (later attempts: previous diffs per region), and to
  a check retry, whose affected regions are resent in groups.
- **Split after a refusal:** a region request with more than one region that the server refuses
  for its per-minute limit, when fewer reply tokens cannot fix it (today an abort), is split
  with the limit the refusal stated and sent again as groups. A single region that does not
  fit still stops the job, as now.
- Groups run like batches: each is spliced over the ones before and checked on the whole file
  (the "changed" check only after the last group of the last batch); regions of later groups
  stay at their original text until their group is sent, so the check of an earlier group does
  not see their rejected versions. A check retry split into groups still counts as one retry:
  if a group of the retry fails its checks, the batch fails. IMPORTS goes with the first group
  (as last returned, editable in every group; the last version wins).
- Trajectory: each batch entry records `groups` (the ids per request) when it was split, and
  the check-retry nudge records `groups` too; a split after a refusal records `split_after_refusal`.

Replay (offline, no model): attempt 4's request for test_suggestions.py, rebuilt with the limit
known (8,000) and the run's ratio (3.96), must go as groups that each fit.

### 3. Attempt budget: set a failed file aside, try the others

**Choice:** a file that fails its checks (after its one check retry, or with a parse error) is
set aside for the rest of the attempt, and the other pending files are still sent. The attempt
is verified only when every pending file was accepted; otherwise it ends `check_failed` once
all files have been tried, and the next attempt sends only the files that failed (with their
rejected candidate and error as now). Attempts stay the one budget.

Why this one rather than a separate per-file check budget:

- It changes only where an attempt stops; requests per attempt stay bounded (each file at most
  once plus its check retry and nudges), and `max_retries` still bounds the whole job. A
  per-file check budget would add a second loop inside an attempt, a new limit to explain,
  and more requests per attempt (the cost limit is what bounds them today).
- It is safe: nothing is verified with a file that failed its checks (that verification would
  only report the file as incomplete), files are still sent in the same order (dependency
  files, non-test, then tests), and test files still see the modules accepted so far (a module
  set aside is "not migrated yet" for the mock-target suggestions, as skipped files are).
- In genai-gptoss-3 it gives: attempt 1 tries all five files (two fail), attempt 2 sends those two,
  so the run reaches verification in attempt 2 at the latest, with two attempts left for
  verifier feedback, instead of none.
- Repeated errors work as now, per file: a file that ends three attempts in a row with the same
  error stops the job at once (no point sending the others when the job cannot verify).

"The attempt counts only once all files have been tried" holds by construction: an attempt no
longer ends at the first failure (only an abort or a repeated-error stop ends it early).

Report: an attempt line lists every failed file (as it lists ok ones today).

### 4. Network blips mid-job

Both providers wait out an unreachable endpoint once it has answered in this job:

- **openai-compat:** a `ConnectFailed` (DNS failure, connection refused, reset before a reply)
  after the endpoint has sent any HTTP response in this job waits `OUTAGE_WAITS_S` (30, 60,
  120 s) between tries, at most three waits per request, then fails as today ("no server at
  ..."), with how long it waited. Before the first response (preflight, a server that is not
  running) nothing changes: refused fails at once, other connect errors get the two short
  retries. Timeouts are still not retried (the server is up). Requests that do not retry
  (model list, `/api/ps`) do not wait.
- **Anthropic:** the SDK's own retries stay; an `APIConnectionError` that is not a timeout,
  after a first successful response, waits the same steps.
- Notes: one job note with the running total, like the rate-limit note: "endpoint unreachable N
  time(s) mid-job; apifix waited S s in total and retried (last error: ...)". The waits count
  in the request's `duration_s`.

### Tests (synthetic fixtures only)

- Scanner: a test file yielding delta-shaped chunks gets [likely] `response_access` findings with
  the chunk suggestion; a non-test file does not; the streaming guide part is selected.
- Unrelated edits: converting such a chunk to SimpleNamespace is accepted once the finding
  exists (a synthetic file shaped like genai's test_generate.py).
- Region edits: a check retry over a known limit is resent in groups that fit; a later attempt's
  request with previous diffs is split; a refused multi-region request is split and resent;
  a single region that cannot fit still stops the job.
- Attempt budget: a failed file is set aside and the next file is still sent; the next attempt
  sends only the failed file; verification once all are accepted; a repeated-error stop still
  stops at once.
- Network: openai-compat waits 30/60/120 s after a first response (fake sleep), records the note,
  succeeds when the server is back, fails after three waits; refused before any response still
  fails at once. Anthropic: same with a fake client.

### Docs

CLAUDE.md: scanner limitation (test-file dict literals: `"delta"` chunks), whole-file mode
(attempts try every pending file; failed files go to the next attempt), region edits (groups
under a known limit, split after a refusal), local/OpenAI-compatible endpoints (outage waits).
README where it describes attempts and network behaviour.

## Built

As planned, with these changes and additions:

- **Stream chunks:** `rules.CHUNK_CHOICE_KEY` and `MOCK_DICT_CHUNK`; a choice holding both
  `"message"` and `"delta"` keeps the response suggestion.
- **Groups:** every request of a region batch runs through one queue (`_Send` items): before
  sending, a request whose built text does not fit the known limit is split; after a refusal
  that fewer reply tokens cannot fix, it is split with the limit the refusal stated. A
  first-send group that fails its checks gets its own retry (the retry budget is per request,
  as it was per batch); a group of a retry that fails ends the batch. Recorded as
  `batches[].splits`.
- **Added: `max_tokens` lowered in advance.** Once a per-minute limit is known, every request
  (both edit modes) whose estimate leaves less room than its `max_tokens` is sent with
  `max_tokens` lowered to fit, when that still leaves the reply estimate
  (`max_tokens_capped_from`). Without it, each group in the replay below was refused once
  before being resent with fewer reply tokens. Two tests that expected later requests to use the
  full budget now expect the cap.
- **Added: test files held back.** The literal "set aside, the others continue" sent test files
  whose module had just failed; their mock-target checks then fail against the module's old
  version (the fixture's test file did, on every existing check-failure test), costing two
  requests and counting toward a repeated-error stop. A test file that reaches a non-test
  module set aside in the same attempt through imports is now held back without a request
  (`attempts[].deferred`); tests that reach it otherwise are still sent.
- **Outage waits:** the quick retries for a reset or DNS failure (1 s, 4 s) still come first;
  refused gets none, so it goes straight to the 30 s wait. The error after the last wait says
  the endpoint answered earlier and how long apifix waited, without the Ollama hint.
- **Found, not fixed:** the undefined-names check reports every new undefined name in one error,
  so a check retry attributes all of them to the region of the first name.

Replays (offline, `~/apifix-runs/genai-gptoss-3`, scratch scripts, no model):

- **tests/test_generate.py, attempt 1's reply:** the scanner now reports the two `yield`ed chunks
  (lines 8 and 19, [likely], the chunk suggestion). With the working copy as it was (generate.py
  accepted), `check_candidate` accepts the reply: every check `ok`, nothing reverted.
- **tests/test_suggestions.py, attempt 4's request** (4 regions, each with attempt 3's rejected
  version as a diff; replies: attempt 3's retry blocks, which fail the mock-target check again),
  against a fake server with the hosted provider's 8,000 TPM and the run's 3.96 characters per token:
  - limit known beforehand: split before sending into [R1, R2, R3] and [R4]; the request and its
    check retry (R1-R3, one group) each ~5,234 prompt + 2,510 reply = ~7,744, sent with the
    lowered `max_tokens`, neither refused. The run's request was 9,425 by the hosted provider's count.
  - limit learnt from the refusal, as in the run: the first request (~6,152 + 6,000) is refused,
    split with reason `refused`, and the same two requests follow, both fitting.
  The batch then fails its retry on the mock target (the replayed replies), as it did in the run:
  the job goes on to the next attempt instead of aborting.
