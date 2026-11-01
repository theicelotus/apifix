# Dropped-cause ordering fix and API key hygiene

## Request (verbatim)

Two fixes.

1. Bug in the dropped-cause check (3d645fb). src/llm/gemini_provider.py in
   agentic-tictactoe has three `raise ... from e` in one scope: ~117 (inside
   `except ValueError as e` around response.text, which the migration
   correctly removes), 142 (`except google_exceptions.GoogleAPIError as e`)
   and 145 (`except Exception as e`). The model's replies kept `from e` at
   142 and 145, yet apifix reported "original line 145: lost its `from e`"
   on all 3 attempts (agentic-tictactoe-genai-8).
   Cause: _dropped_causes matches causes by text first (117 and 142 consume
   the two `e`s) and only applies _cause_handler_gone to what's left (145,
   whose clause still exists). Apply _cause_handler_gone first: drop the
   original raises whose except clause is gone, then match the rest.
   Test: three raises in one method; candidate removes the ValueError clause
   and keeps the other two `from e` -> no error; candidate also drops 145's
   `from e` -> one error at that line.

2. API key hygiene, for any provider: strip surrounding whitespace from the
   key read from the environment. If it's empty after stripping, fail
   before preflight with "the API key in $VAR is empty" instead of sending
   a request. A 401 from preflight should say "the server rejected the key
   in $VAR (starts <first 4 chars>…)" so a wrong or mismatched key is
   obvious. Never print more than 4 characters of a key.

Tests for each. Write the plan doc. Run the full suite once (whole suite),
then ruff. Commit without Co-Authored-By lines and push to origin main.

## Plan

1. `_dropped_causes` (engine/unrelated.py): per scope, first set aside the original raises
   whose `except` clause is gone (`_cause_handler_gone`), then match the remaining causes by
   text, then any spare. Previously the gone raise (117) and a kept one (142) consumed the two
   candidate `e`s, leaving 145 unmatched.
2. `read_api_key(var)` and `rejected_key_message(var, key)` in engine/llm.py. Unset -> None
   (each provider decides whether that is fatal); set but blank after `.strip()` -> `LLMError`
   "the API key in $VAR is empty", raised from the provider constructor, before any request.
3. Both providers use them. openai-compat adds the rejected-key text to its 401 (server message
   kept, already scrubbed of the key); the Anthropic provider does the same for the SDK's
   `AuthenticationError`. At most 4 characters of a key are ever shown.

## Built

- Cause ordering fixed; tests in tests/test_drift.py use a three-raise method (fail before the
  fix, pass after).
- Key stripping, blank-key error and 401 message in both providers, with tests in
  tests/test_llm.py and tests/test_llm_openai_compat.py.
- Behaviour change: an openai-compat key variable that is set but blank (`APIFIX_LLM_API_KEY=`)
  used to mean "no key"; it is now an error. Unset still means no key (local servers).
- Anthropic has no preflight call; its 401 surfaces on the first request.
