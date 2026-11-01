# Control-flow drift, the ResourceExhausted guide pattern, weak retry vs import rot

## Request (verbatim)

Control-flow drift from ~/apifix-runs/agentic-tictactoe-genai-9 (fix.diff,
src/llm/gemini_provider.py ~line 205). Original:
    except google_exceptions.ResourceExhausted as e:
        retry_after = None
        if ...: retry_after = headers.get("Retry-After")
        if retry_after: wait_time = float(retry_after)
        else: wait_time = 2**attempt
        if attempt < max_retries - 1: time.sleep(wait_time); ...; continue
        raise
The fix wrapped part of it in `if getattr(e, "code", None) == 429:` and
re-indented, which moved `retry_after = None` inside that if (NameError for
other 4xx) and the `if attempt < ...: time.sleep(...)` block into the else
of `if retry_after:` (Retry-After never honored). Tests and checks missed it.

1. Check: for each original statement outside finding lines, compare its
   chain of enclosing blocks (each if/else branch, loop, try/except, with
   the condition text) with the candidate's. A new wrapper the migration
   adds is fine. A statement that moves into an existing branch it was not
   in (here: into the else of `if retry_after:`) is reported as an
   unrelated control-flow change. So is a variable whose only assignment
   moves into a new conditional while it's still used after it.
2. Guide (errors part): for ResourceExhausted -> ClientError, show the
   pattern that keeps the body as it is:
       except errors.ClientError as e:
           if e.code != 429:
               raise
           <original ResourceExhausted body, unchanged>
3. Weak retry: skip the extra attempt when baseline_import_rot explains the
   still-failing tests (genai-9 spent attempts 7-8 on 40 tests blocked by
   a pydantic-ai import error).

Tests for each (a fixture shaped like this block). Write the plan doc. Run
the full suite once (whole suite), then ruff. Commit without
Co-Authored-By lines and push to origin main.

## Plan

1. `unrelated.py`, two checks added to `_drift` (they run on the whole file, not only on hunks):
   - `_moved_statements`: simple statements pair up by scope and text (when the counts match);
     each carries its chain of enclosing blocks (`if`/`else`, loops, `except` clauses; `with`
     does not change the chain; `try` body/else/finally never count as "existing"). A block in
     the candidate's chain that is not in the original's is a new wrapper (fine) unless the
     original has a block with the same header text and branch (then the statement moved into
     an existing branch: reported, once per block). Statements on finding lines are skipped.
   - `_stranded_variables`: a name the original assigns that the candidate assigns only inside
     one new `if`/loop/`except` block, while still reading it outside that block. Names
     assigned on finding lines are skipped.
2. `google_genai/guide.md` errors part: the `except errors.ClientError` + `if e.code != 429:
   raise` pattern, body unchanged.
3. `weak_retry.import_rot_tests`: the still-failing tests whose fixed-run output names a changed
   module, when the verification has a `baseline_import_rot` reason (the reason itself lists only
   3 tests, so the output is matched again). `_extra_attempt` drops them; if none are left it
   records `not retried: baseline_import_rot: ...`. `verifier._rot_module` became `rot_module`.

Tests: `tests/test_unrelated.py` (fixture shaped like the genai-9 handler: the guide pattern
passes, the wrapped version gives one moved-statement and one stranded-variable error),
`tests/test_weak_retry.py` (end-to-end skip + unit test of `import_rot_tests`).

## Built

As planned. Limits: a statement that moves *out* of a block is not reported; statements whose
text occurs a different number of times in the two versions are not paired; a new wrapper whose
header text equals an unrelated existing block's can be mistaken for a move.
