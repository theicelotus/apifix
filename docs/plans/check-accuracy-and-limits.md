# Plan: check accuracy, token limits and small fixes (from the google-genai reruns)

Evidence: ~/apifix-runs/ailabel-genai-2, agentic-tictactoe-genai-2 (an 8,000 tokens-per-minute
endpoint), agentic-tictactoe-genai-3 (low reasoning) and -genai-4 (default reasoning) on another
OpenAI-compatible endpoint. No provider is named in the repo.

## Request (verbatim)

A. Check accuracy (most important). genai-2 passed src/llm/gemini_provider.py
   on attempt 1; genai-3 and genai-4 failed it 3 times each. From the
   trajectories, compare the accepted reply with the rejected ones and fix
   any false positives:
   1. Key check: the model wrote
      genai.Client(api_key=self._api_key or os.getenv("GEMINI_API_KEY"))
      for configure(api_key=self._api_key). The key still reaches the client.
      Accept the original key as an operand of or / a conditional / a local
      assigned from it; still reject a rewrite that drops it.
   2. String literals 'candidates', 'finish_reason' (original lines 107-111)
      were reported missing. Look at the original: if they are attribute
      names used via getattr/hasattr/dict access on the old response, and
      the rewrite uses the same attributes directly, that is not a dropped
      string. Fix the check if so; if the rewrite really lost them, say so.
   3. repeated_check_error said "the same error on attempts 1, 2 and 3",
      but attempt 1's listed errors don't include the key check. Only stop
      early when the same error really repeats, and don't truncate the
      error list that decides it.
   Keep the real catches (deleted retry guard at line 223, added
   wait_time = 2**attempt): they must still fail.

B. Token limits (from genai-2, 8,000 TPM):
   4. Stop early when the same file is cut off at the output limit with no
      smaller request left on 2 attempts in a row; say the per-minute limit
      leaves too few reply tokens and suggest a higher-limit endpoint.
   5. A single-region request for tests/unit/llm/test_gemini_provider.py was
      ~6,000 prompt tokens. Report what those tokens were and cut the
      largest part a single region doesn't need.

C. Small fixes:
   6. Import-to-package names: "ruamel" -> ruamel.yaml, "pythonjsonlogger"
      -> python-json-logger. Fix the mapping generally (a table of known
      import/distribution mismatches), not one name at a time.
   7. ailabel-genai-2's report lists 9 "apifix reverted" lines showing ""
      restored. Show the real line text, or say "blank line".
   8. When preflight can't find the model, don't put the server's whole
      model list (~400 names) in the outcome/report: show the server's
      message plus at most ~10 close matches.

Tests for each, with fixtures shaped like these cases. Write the plan doc.
Run the full suite once, then ruff. Commit each group without
Co-Authored-By lines and push to origin main.

## What the trajectories showed

- **Key check (1).** In genai-3 and genai-4 every one of the 6 attempts failed the `key` check
  (`configure(api_key=self._api_key)` -> `genai.Client(api_key=self._api_key or os.getenv(...))`
  or `effective_key = ... if ... else ...`). The check required a `Client(api_key=...)` whose
  value was the identical expression. A false positive on every reply, so it decided the run.
- **Strings (2).** Reading the rejected diffs: the genai-3 and genai-4 rewrites replaced
  `try: text = response.text / except ValueError` with `if response.text is not None`; in
  genai-3 attempt 2 the diff shows the whole safety branch deleted. That reply really lost
  `'SAFETY'` and the "blocked ... safety filters" message (kept as errors). But `'candidates'` and `'finish_reason'`
  are attribute names read through `hasattr(response, "candidates")` and
  `getattr(candidate, "finish_reason", None)`; a rewrite that reads `response.candidates` /
  `candidate.finish_reason` directly kept them as attributes, and the check still called them
  dropped strings. The trajectories store no reply text, so which individual replies did this is
  not recoverable; the check now exempts that case and still reports the real losses.
- **Repeated error (3).** The streak logic was right: the key error did repeat on attempts 1, 2
  and 3. What hid it is the report: each attempt's line cut its error list at 300 characters, so
  attempt 1's line ended in "...fin..." before the key error (the last of 6), and the stop reason
  cut the error at 160 characters. Both now show every error whole. (The stop was a true repeat
  of a false positive; fixing the false positive is what removes it.)
- **Request size (5).** The single-region request (R1, 19,135 + 8,714 system characters, 6,529
  tokens): the previous-version note 6,937 chars (25%), of which all of it was the check errors
  of regions R4-R15, none of them in the request; the changed statements of the rewritten
  module 7,966 chars (29%); the guide in the system prompt 6,738 chars (24%); the region
  itself 660. The note's error text came from the whole file's failed reply and was copied to
  every group of the next attempt's first request.

## Plan

A. `google_genai/config_checks.py`: the key check accepts a `Client(api_key=v)` when the
   original key expression is `v` or any operand inside it (`or`, a conditional, a call), or `v`
   is a local name assigned from such an expression (followed through chains of locals); a
   rewrite with `Client()`, another expression, or a local not derived from the key still fails.
   `unrelated._key_constants`: also `getattr/hasattr/setattr/delattr(x, "name")` and
   `d.get("name")` names, exempt when the rewrite uses `.name` as an attribute (the existing rule
   for dict keys). `agent.py`: the report shows each failed file's errors whole (the first line
   of each bullet), the stop reason shows the whole error.
B. `agent.py`: `_count_cutoffs` stops the job (outcome `output_limit`) when one file's reply is
   cut off with no smaller request left on 2 attempts in a row (the advice names the per-minute
   limit when it capped the reply, else `--max-output-tokens`). Region requests: a group sent
   after a failed reply gets only its own regions' errors (and those naming no region), both
   when split from a retry and for the first request of the next attempt (`wf_region_errors`).
C. `dependencies.IMPORT_DISTRIBUTIONS` grows to the known mismatches (ruamel, pythonjsonlogger
   and about 40 more), with a test that the table is normalised; `revert_text` words a
   whitespace-only revert as "whitespace changed on a blank line" / the line's text;
   `llm_openai_compat._listed` shows the server's message plus the 10 closest models.

## Built

(filled in per group below)

### A (checks): key check follows `or`/conditionals/locals; getattr/hasattr/.get names exempt when the rewrite reads them as attributes; errors shown whole in the report and stop reason.
### B (limits): `output_limit` stop after 2 consecutive no-way-out cut-offs; region groups carry only their own regions' errors.
### C (small): wider import/distribution table, whitespace reverts worded, model list cut to 10 closest.
Full suite: 2146 passed; ruff clean.
