# finish_reason int comparisons and added trailing whitespace

## Request (verbatim)

A. google-genai scanner rule + whitespace (engine change)
1. Comparisons of `finish_reason` (or `.finish_reason`) with an int literal
   (`== 2`, `!= 3`, `in (2, 3)`) are findings: in google-genai FinishReason
   is a string enum, so the comparison is always False (kaizen-agent's
   kaizen/autofix/code/llm_fixer.py: `if finish_reason == 2:  # Safety`
   silently stopped working). Suggest the enum member
   (types.FinishReason.<NAME>). Verify the old proto ints -> new names
   mapping against the installed google-genai and old SDK before writing it;
   say what you found.
2. Strip trailing whitespace the model adds on lines it writes
   (a kaizen-agent run's fix.diff has 9 such lines). Don't
   touch lines the model didn't change.
Tests for both. Write the plan doc. Run the full suite once, then ruff.

## Plan

- `google_genai/python_checks.py`: `visit_Compare` reports `finish_reason` / `x.finish_reason`
  compared (`==`, `!=`, `in`, `not in`, either side) with an int or a tuple/list/set of ints, in
  files that use the old SDK. Certain, `response_access`. The suggestion names the enum members.
- `google_genai/rules.py`: `FINISH_REASON_NAMES` (int -> member) and the suggestion text.
- Mapping checked by installing google-generativeai 0.8.6 and google-genai 2.34 and listing
  both enums.
- `diffing.strip_added_trailing_whitespace(original, fixed)`: rstrip spaces/tabs on lines the
  fix inserted or replaced (via `changed_lines`), keep line endings, skip `.md/.rst`
  (trailing double space is a line break). Called in the agent loop before each verification,
  so the tested tree is the shipped tree.

## Findings from the mapping check

Old proto ints: 0 UNSPECIFIED, 1 STOP, 2 MAX_TOKENS, 3 SAFETY, 4 RECITATION, 5 OTHER,
6 LANGUAGE, 7 BLOCKLIST, 8 PROHIBITED_CONTENT, 9 SPII, 10 MALFORMED_FUNCTION_CALL,
11 IMAGE_SAFETY. The new `types.FinishReason` is a `str` enum whose values equal its names
(`FinishReason.STOP == "STOP"`, `FinishReason.STOP == 1` is False), and has more members
(UNEXPECTED_TOOL_CALL, ...). Note 5 is OTHER and 6 LANGUAGE (not the reverse). In the old SDK
`== 2` means MAX_TOKENS, so kaizen-agent's `# Safety` comment was wrong even before migrating
(SAFETY is 3); the suggestion lists the real mapping so the human can decide.

## Built

As planned. Tests: `test_finish_reason_compared_with_int`,
`test_finish_reason_int_needs_the_old_sdk` (tests/test_google_genai_scanner.py, fixture
`finish_reason_int.py`), `test_strip_added_trailing_whitespace_only_touches_changed_lines`
(tests/test_diffing.py).
