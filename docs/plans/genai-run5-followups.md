# Plan: follow-ups from agentic-tictactoe-genai-5

Requested 2026-10-31, to be built directly. The request is verbatim.

## Request

Follow-ups from ~/apifix-runs/agentic-tictactoe-genai-5. src/llm/gemini_provider.py
now passes on attempt 1; tests/unit/llm/test_gemini_provider.py failed the
string check 3 times with "original line 93: the string 'Test prompt'
is missing", and once with "'safety_settings' is missing" (line 90).

1. Save the model's replies: the trajectory must keep each reply's text
   (or write them to replies/ next to it, one file per request), so a
   rejected rewrite can be inspected afterwards. Keep it local-only output,
   like the rest of the run folder.
2. String check, dict keys: a string used as a subscript key or .get() key
   (call_args[1]["safety_settings"], kwargs.get("contents")) is kept when
   the rewrite uses the same name as a keyword argument (safety_settings=)
   or attribute (.safety_settings). Extend the getattr/hasattr exemption
   to this.
3. String check, 'Test prompt': rebuild the genai-5 attempt 3-5 test file
   from what the run kept if you can (if not, build a fixture shaped like
   original lines 88-95 migrated to models.generate_content(model=...,
   contents="Test prompt", config=...)). If the literal is present anywhere
   in the migrated test, the check must pass: make sure it isn't matching
   by line position or only within the same call.
4. Feedback that repeats: when the same check error comes back on a second
   attempt, include the model's own line at that spot in the message
   ("you wrote: ...") so it can see what to change.

Tests for each. Write the plan doc. Run the full suite once, then ruff.
Commit without Co-Authored-By lines and push to origin main.

## What the run showed

- Original line 90 is `assert "safety_settings" in call_args[1]`: a membership test. The
  subscript and `.get()` forms the request names were already exempt (`_key_constants`); the
  `in` form was not, so that is the form that failed.
- The run kept the replies in `files[].messages` (whole-file mode), so attempts 3-5 of the test
  file could be rebuilt. In all three, the model deleted the `assert_called_once_with("Test
  prompt", ...)` assertion outright: `Test prompt` appears once (the `generate(prompt=...)`
  call) where the original has it twice. The check was right to flag it; it counts string
  values over the whole file (a multiset), by neither line position nor call.
- Attempt 5's first reply also dropped `max_output_tokens` and `safety_settings` entirely.

## Plan

1. Replies: each response's text goes to `replies/attempt-<n>-response-<i>.txt` (i is the index
   in the attempt's `responses`, as `replies[].response` uses it), written by the CLI next to
   trajectory.json, home dir redacted. The trajectory names the file (`reply_file`) and does not
   hold the text. `FixResult.replies` carries them; `apifix pr` reads a fixed list of run files,
   so the folder stays local.
2. Keys: `"name" in x` / `"name" not in x` joins subscript, `.get()`, `getattr`/`hasattr` and
   dict keys in `_key_constants`. Each is kept when the rewrite has `name` as an attribute or
   keyword.
3. 'Test prompt': tests on a fixture shaped like original lines 78-95, plus a test that follows
   the real shape (the assertion removed). The multiset rule is unchanged.
4. Repeated feedback: when a failed reply has an error an earlier reply for the file had too
   (same `error_key`, any attempt or retry), the retry or next-attempt request adds "These
   errors came back. Your own lines at those spots:" with the model's lines there. `original
   line N` maps to the candidate through a line diff; `line N` is already a candidate line.
   Whole-file mode only; region batches are not covered.

## Built

- `agent.py`: `Attempt.reply_texts`, `FixResult.replies`, `reply_file()`; each response entry in
  the trajectory has `reply_file`. `cli.py`: `_write_outputs` writes `replies/` (redacted).
- `unrelated.py`: `_key_constants` covers `"name" in x` / `not in`.
- `whole_file.py`: `own_lines_note`; `agent.py` adds it to the check retry and to the next
  attempt's request for errors that an earlier reply for the file also had
  (`wf_repeat_errors`).
- Tests: test_unrelated.py (key forms, the genai fixture, literal kept anywhere / removed),
  test_whole_file.py (own_lines_note), test_agent.py (replies kept, "you wrote" in the retry),
  test_cli.py (replies written and redacted).
- Full suite with --docker: 2168 passed; ruff clean.

Known gaps: the 'Test prompt' failure was a real removal, so the rule is unchanged; a literal
hoisted into one variable (two uses become one literal) is still reported, as the count is per
value. Region batches get no "you wrote" lines.
