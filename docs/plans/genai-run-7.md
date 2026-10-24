# genai run 7: streaming check on dict-typed deltas

## Request

Context: Read CLAUDE.md and docs/plans/genai-run-6.md. genai run ~/apifix-runs/genai-gptoss-7/ (gpt-oss-120b, reasoning low) stopped with repeated_check_error: genai/generate.py failed the streaming check on attempts 1, 2 and 3 ("content" in delta, lines 60, 55, 55). genai’s deltas() handles dict-typed deltas. Record this request verbatim in docs/plans/genai-run-7.md, then:

Diagnose from the trajectory: show me the flagged lines with their surrounding branch in each of the three replies. Is "content" in delta inside a branch where delta is known to be a dict (isinstance(delta, dict), a Mapping check, a TypedDict annotation, or a value that comes from .model_dump() / json.loads)? If so, the check is a false positive: exempt those cases and add tests. If not, the model really kept the bug: make the message include a 3–4 line before/after example built from the flagged code, as the other checks do.
Was any other check (attempt 1’s “changed code unrelated to the migration” on messages = []) also wrong? Say which.
Replay the three replies against the changed check and report which would now be accepted.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

From `~/apifix-runs/genai-gptoss-7/trajectory.json` (`openai/gpt-oss-120b` on a hosted endpoint,
`--reasoning-effort low`). Only genai/generate.py was ever sent: it failed in each attempt, so
the four test files were held back each time. Each attempt had two replies (the request and its
check retry); attempt 2's two replies are identical, as are attempt 3's.

The flagged line in every reply, with its branch (attempt 1, line 60 of the checked file; the
same code in attempts 2 and 3 at line 55, only comments differ):

```python
for chunk in completion:
    # Support both the new object style and legacy dict style in tests.
    if isinstance(chunk, dict):
        delta = chunk["choices"][0]["delta"]
        if "content" in delta:  # <- flagged
            yield delta["content"]
        continue

    # New object style.
    if not getattr(chunk, "choices", None):
        continue
    delta = chunk.choices[0].delta
    if delta.content is not None:
        yield delta.content
```

- **The streaming check was a false positive.** `"content" in delta` sits inside `if
  isinstance(chunk, dict):`, and `delta` is assigned in that branch by subscripts of `chunk`:
  it is a dict, where a key check is the right test. The object path below it already does what
  the check asks (`if delta.content is not None:`). The detector is flow-insensitive per scope,
  so the dict branch's `delta` counted as a stream delta like the object branch's. The branch is
  dead in 1.x for real streams (chunks are pydantic models) and only serves the tests' dict
  mocks, which the scanner flags separately (MOCK_DICT_CHUNK), so keeping it hides nothing.
- **Attempt 1's unrelated-edit error was right.** The reply turned `messages = []` (original
  line 92, in generate_exception_suggestion, far from any finding) into `messages: List[Dict[str,
  str]] = []`: a type annotation the migration does not need. The check retry kept it, so both
  replies failed it correctly. The other change apifix made to attempt 1 (reverting the added
  comment "# TypedDicts retained for type checking ...") was also right. No other check
  failed in attempts 2 and 3.

## Plan

1. Exempt `"content" [not] in D` where D is known to be a dict, in `streaming.content_checks`
   (shared by the scanner, the rewrite check and so the verifier's completeness check, which
   would otherwise still see a finding left in an accepted fix):
   - dict guards (`isinstance(G, dict)`, `Mapping`, `MutableMapping`, any module prefix, a
     tuple of only those) on D or on the name D is read from through subscripts / attributes,
     following names assigned inside the guarded code (else in the scope): `if`/`elif` bodies,
     the `else` of `if not isinstance(...)`, statements after `if not isinstance(...):
     continue/break/return/raise`, later operands of `isinstance(...) and ...`, a conditional
     expression's first branch;
   - dict sources: `json.loads` / `json.load`, openai's `to_dict()` / `to_json()` (they leave
     unset fields out by default, so a key check behaves as on 0.x's dicts), `model_dump()` /
     `model_dump_json()` only with `exclude_unset=True` or `exclude_none=True`.
2. Not exempt: plain `model_dump()` (pydantic keeps `content: None`, so the key check is
   always true: the same bug as `hasattr`), `to_dict(exclude_unset=False)`, `hasattr` checks,
   and TypedDict annotations. The last one deliberately differs from the request: genai's own
   0.x code annotates its chunks (`deltas(completion: Iterator[StreamCompletion])` with TypedDict
   `Delta`), so an annotation exemption would drop the scanner's real finding on the original;
   an annotation says what the code expected in 0.x, not what 1.x passes it.
3. The before/after example in the message is not built: the condition for it (the model really
   kept the bug) did not hold.
4. Tests on synthetic code (each guard form, each source, the cases that stay flagged) and on
   the stream_deltas fixture (a both-styles rewrite of deltas() passes the check and leaves no
   scanner finding; the same code without the guard still fails).
5. Replay the six replies through `check_candidate` with the run's settings (findings, rewrite
   checks).

## Built

As planned. Known miss, documented in CLAUDE.md: 0.x code whose only stream handling sits under
`isinstance(chunk, dict)` (0.x chunks were dict subclasses) loses its key-check finding; its
call is still flagged.

Replay (`replay_run7.py`, offline, genai's original from ~/apifix-targets/genai, the run's six
generate.py replies, `check_candidate` with the file's findings and the migration's rewrite
checks):

| Reply | Before | Now |
|---|---|---|
| attempt 1, request | unrelated_edits + streaming | rejected: unrelated_edits (`messages = []`) |
| attempt 1, check retry | unrelated_edits + streaming | rejected: unrelated_edits (`messages = []`) |
| attempt 2, request | streaming | **accepted** |
| attempt 2, check retry | streaming | **accepted** |
| attempt 3, request | streaming | **accepted** |
| attempt 3, check retry | streaming | **accepted** |

The scanner finds nothing left in any of the six (completeness would pass). In the run, attempt
2's first reply would have been accepted, and the four test files sent in the same attempt.
