# Drift check: `from e` of a removed except clause; guide for `.text` and retry loops

## Request (verbatim)

False positive from the new drift check (~/apifix-runs/agentic-tictactoe-genai-7,
replies/ has the text). src/llm/gemini_provider.py: the original does
    try: text = response.text
    except ValueError as e: raise ...(...) from e
because the old SDK raised ValueError on a blocked response. The new SDK
returns None instead, so the correct migration is
    text = response.text
    if text is None: raise ...(...)
and `from e` has nothing to refer to. apifix rejected that 3 times
("`raise ... from e` lost its `from e`"). Confirm from replies/.

1. Drift check: allow dropping `from X` when the except clause that bound X
   was removed or replaced as part of the migration (its exception type is
   one the new SDK no longer raises there, or the try/except itself is gone).
   Still reject dropping `from X` while that except clause remains.
2. Guide (responses section): state that response.text returns None instead
   of raising ValueError, show the migration above (keep the raise and its
   message, keep the safety/finish_reason handling), and say not to change
   retry loops: only exception class names change (google_exceptions.X ->
   errors.ClientError/ServerError/APIError), not the loop, its guards or
   its waits.

Tests for each (fixture shaped like this file). Write the plan doc. Run
the full suite once (whole suite), then ruff. Commit without
Co-Authored-By lines and push to origin main.

## Findings

Confirmed from replies/: all attempts keep the raise under `if text is None:` with no
`except ValueError` left; the only remaining chained raises are in `except errors.APIError as e`
and `except Exception as e` clauses. `_dropped_causes` (engine/unrelated.py) matched causes by
text only, so the removed clause's `from e` was reported as lost.

## Plan

- `_dropped_causes`: before reporting an unmatched cause, `_cause_handler_gone` finds the
  original `except ... as X` clause around the raise. The cause is not owed when the candidate
  has no clause in the same scope binding X with the same exception type, unless the original
  type is not a builtin (a renamed library exception such as `google_exceptions.X` ->
  `errors.ClientError` is still the same clause). A builtin type that changed
  (`ValueError` -> `Exception`) counts as replaced.
- Guide, `responses` part: the `.text` -> `None` check with a before/after example (keep the
  raise, its message, the safety / `finish_reason` handling; `from e` goes with the removed
  clause). `errors` part: do not change retry loops; only exception class names change.
- Tests: tests/test_drift.py fixture shaped like gemini_provider.py (removed clause allowed; still
  rejected while a renamed library clause or the same clause remains);
  tests/test_google_genai_guide.py for both guide texts.

## Built

As planned. The retry-loop sentence went in the `errors` part (where the exception renames
live) rather than `responses`, so it is sent whenever the renames are.
