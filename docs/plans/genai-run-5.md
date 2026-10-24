# genai run 5: truncated replies and refusals under a known limit, --reasoning-effort, agent-mode autospec hint

## Request

Context: Read CLAUDE.md and docs/plans/genai-run-3.md. genai run ~/apifix-runs/genai-gptoss20b-1/ (model openai/gpt-oss-20b on a hosted endpoint, 8,000 TPM). Record this request verbatim in docs/plans/genai-run-5.md, then build it.

Truncated replies under a known limit: in attempt 1, tests/test_generate.py (whole) and tests/test_suggestions.py (regions) hit the output limit, probably because gpt-oss’s reasoning tokens count against max_tokens, which apifix had lowered to fit the limit. A truncated reply should not fail the file when a smaller request is possible: retry within the same attempt with fewer regions per group (down to one region), or as regions instead of a whole file, and only fail once a single region still can’t fit. Record each split.
Refused twice: attempt 2’s request for tests/test_suggestions.py was refused, sent again with max_tokens lowered from 6,000 to 4,619, refused again (9,125 against 8,000), and the job aborted. A request that still doesn’t fit after lowering must be split into smaller groups, never abort while a smaller split exists. Find why the splitting from genai-run-3.md didn’t happen on this path (check retry after a truncated attempt?). Replay to confirm.
--reasoning-effort low|medium|high for openai-compat: sent as reasoning_effort in the request body (the hosted provider supports it for gpt-oss models). Omit it by default. If the server rejects the parameter, say so clearly. Record it in the report and trajectory, and include reasoning tokens (if the server reports them) in the usage figures.
Agent-mode autospec hint (the decision from the last commit): add a failure hint keyed on Mock object has no attribute 'completions' with the same advice as the autospec check.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

From `~/apifix-runs/genai-gptoss20b-1/trajectory.json` (`openai/gpt-oss-20b` on a hosted endpoint, 8,000 TPM,
`--max-output-tokens 6000`):

- **No limit was known in attempt 1.** The first per-minute refusal came in attempt 2, so the
  two truncated replies were not caused by `max_tokens` lowered to fit a limit. They were sized
  by `reply_budget` and the 6,000 cap:
  - `tests/test_generate.py` (1,099 bytes, whole): `max_tokens` 2,782 (`2 * 367 + 2,048`). The
    reply held 13,093 characters of reasoning and 263 of code: the reasoning used up the
    budget's margin. 3,218 more reply tokens were available under `--max-output-tokens`.
  - `tests/test_suggestions.py` (4 regions, 7,561 bytes): `max_tokens` 6,000, the cap. 26,800
    characters of reasoning and no content at all.
  So the request's "smaller request" alone would not have saved test_generate.py: as regions
  its reply budget would have been smaller still. The first fix for a cut-off reply is more
  reply tokens, when there are any left; only then a smaller request.
- **Refused twice.** Attempt 2's request for test_suggestions.py (all 4 regions, max_tokens
  6,000) was refused with "Requested 9,125"; apifix took the server to count prompt +
  max_tokens (prompt 3,125), lowered max_tokens to 4,619, and the hosted provider refused again with
  *the same* 9,125. the hosted provider's count did not depend on max_tokens at all (in attempt 1 it had
  accepted the same regions at 5,132 prompt tokens + 6,000). `_fitting_max_tokens` then raised
  `_CannotFit` with the limit, and the region queue tried to split the request
  (`requeue(send, "refused")`), as genai-run-3 built.
- **Why no split happened** (not a check retry; it was the batch's first request, carrying the
  145-character note on attempt 1's truncation): `split` packs groups by apifix's estimate,
  prompt characters / 3.94 plus the reply estimate (regions / 3). With the smallest context,
  all four regions came to 5,006 + 2,587 + 256 = 7,849 <= 8,000: one group, so `requeue`
  returned False and the abort went through. The server had just counted that request at
  9,125, and nothing fed that back into the estimate. Reproduced offline (replay below): the
  same 6,000 -> 4,619 sequence and the same abort message.

## Plan

1. **Cut-off replies** (whole-file mode, both forms), within the attempt, recorded per file entry
   as `truncations`:
   - The same request again with the most reply tokens a request may have
     (`--max-output-tokens`, capped to a known limit as every request is), when that is more
     than it had. The file keeps that allowance for the rest of the job (a later attempt would
     be cut off the same way).
   - Already at the most: a whole file goes as regions (auto only, and only when the regions
     and IMPORTS are under 80% of the file, else the reply would not shrink); a region request
     with several regions is split in halves by bytes (IMPORTS with the first), each half with
     the full allowance; a half that is cut off is halved again.
   - A single region (or a whole file with no smaller form) cut off at the full allowance fails
     the file as before, with the allowance in the error, and the report suggests
     `--reasoning-effort low` for openai-compat.
2. **Refused again after lowering:** the refusal teaches the job how much more the server counts
   than apifix estimates (`server_ratio`: the server's count, or its count with max_tokens
   replaced by the reply estimate when the count moved with max_tokens, over apifix's
   estimate). Every estimate against the limit (`_fits`, batch planning) is scaled by it for
   the rest of the job, so the split after the refusal produces groups, and later requests are
   split before they are sent instead of being refused twice. If the scaled estimate still
   packs every region into one group, the regions go in halves: the server said it does not
   fit. A refused group is split again; only a single region stops the job (a whole file still
   switches to regions first). A refusal whose count already leaves room for the reply (the
   server does not count it) is handled the same way instead of stopping the job.
3. **`--reasoning-effort low|medium|high`** (openai-compat only; anthropic: usage error): sent as
   `reasoning_effort` in every request body, preflight included, omitted by default. A 400/422
   whose message names reasoning_effort while it is set: "the server rejected
   reasoning_effort=...: model M or this server does not accept --reasoning-effort; run without
   it". `job.reasoning_effort` in the trajectory, on the report's Model line. Reasoning tokens
   from `usage.completion_tokens_details.reasoning_tokens` go into `Usage.reasoning_tokens`
   (a part of output_tokens, not added to the total), summed per attempt and job, named on the
   report's Tokens line.
4. **Autospec hint:** `failure_hints` gains `Mock object has no attribute 'completions'` with the
   autospec check's advice. Agent mode's feedback after a failed verification did not pass the
   migration's hints at all (only whole-file mode did); it does now, so agent mode also gets the
   API-key and httpx `proxies` hints.

## Built

As planned. Replays (offline, scratch script, no model: the run's replies per file in order,
fresh requests answered by echoing their regions unchanged; the hosted provider emulated as counting a
request at apifix's prompt estimate + 3,892, the offset that gives the run's 9,125, whatever
max_tokens is):

- **Before the change, refusals only on test_suggestions.py's second request:** attempt 2 is
  refused at 9,125 with max_tokens 6,000 and again with 4,619, then aborts with the run's
  message.
- **After, the run's own replies (no refusals in attempt 1, as in the run):** test_generate.py,
  cut off at 2,782, is sent again at 6,000 and the run's attempt 2 reply comes back complete
  (it then fails the autospec check, as in the run). test_suggestions.py, cut off at 6,000
  (already the most), goes as [R1, R2] and [R3, R4], each at 6,000
  (`splits: reason truncated`); both accepted. test_generate.py's check retry is accepted in attempt 2, and
  the job is verified there (a fake sandbox) instead of aborting.
- **After, the hosted provider's count applied to every request:** test_suggestions.py's 4-region request is
  refused at 9,088 (max_tokens 6,000), lowered to 4,656 and refused again at 9,088;
  `server_ratio` becomes 1.17 and the request is split into [R1, R2] and [R3, R4]
  (`reason refused`), counted at 7,885 and 7,786, both accepted by the emulated server.
  [R1, R2] then gets the run's cut-off reply and is halved to [R1] and [R2]
  (`reason truncated`). The job continues instead of aborting.
