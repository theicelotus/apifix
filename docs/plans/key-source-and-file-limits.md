# Plan: keep the repo's key source, and one flag for the whole-file size limits

Requested 2026-10-31, from the run in `~/apifix-runs/kaizen-agent-genai-3` (`replies/` has the
model text). The request is verbatim.

## Request

1. Guide scope (root cause): the key-precedence rule (GEMINI_API_KEY then GOOGLE_API_KEY) made
   the model rewrite repo code that reads its own variable. kaizen/autofix/pr/manager.py:
   `api_key = os.environ.get("GOOGLE_API_KEY") ... configure(api_key=api_key)` became
   `client_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")` in all 3
   attempts (see replies/attempt-*-response-{2,3,5,6}.txt). Limit the rule to configure() calls
   with no api_key. When the repo passes its own key, the guide must say: keep how the repo
   reads it, unchanged; pass the same variable to genai.Client(api_key=...). Send that rule only
   when it applies.
2. Key-check feedback: when the key source changed (different env vars or an added fallback that
   can win over the original), say that plainly: "the original reads the key from
   os.environ.get("GOOGLE_API_KEY"); keep that exactly, don't add GEMINI_API_KEY". Keep
   accepting `original_key or fallback` (original still wins), as in agentic-tictactoe; keep
   rejecting a fallback placed before the original.
3. --max-file-bytes 80000 was passed, but the report says "kaizen/autofix/code/llm_fixer.py
   (66,499 bytes, over 8,000)" and it was split into regions. Find which limit that 8,000 is,
   make --max-file-bytes (or a clearly named flag) control it, and name the limit in the report.

Tests for each. Write the plan doc. Run the full suite once, then ruff. Commit and push to
origin main.

## Plan

1. `guide.md`: the "Client" part keeps only "never drop a key value". Two new sub-parts:
   `client.key.own` (the repo passes `api_key=X`: keep how it reads X, pass the same expression)
   and `client.key.env` (no `api_key`: the GEMINI/GOOGLE precedence swap, now said to apply
   only there). `guide_parts.selected_parts` parses each `configure` finding's `old_pattern` and
   adds `own` if any call passes `api_key`, `env` if any does not; neither without a configure
   finding.
2. `config_checks._key_errors`: read the env vars the original key reads (following local
   names). A rewrite that drops the key but reads other variables, or that tries a variable the
   original did not read before the original key, gets the plain message naming the original
   read and the added variables. `original or fallback` and a conditional with the original
   first stay accepted.
3. The 8,000 is `DEFAULT_REGION_THRESHOLD` (`--region-threshold`, files over it are sent as
   regions in `--file-edits auto`), separate from `--max-file-bytes` (24,000). Now
   `--max-file-bytes` also sets the region threshold unless `--region-threshold` is given. The
   report names the flag: "over the --region-threshold of N" / "over the --max-file-bytes limit
   of N".

Tests: `tests/test_google_genai_followups.py` (guide parts), `tests/test_check_accuracy.py`
(key check), `tests/test_cli.py` (limits), `tests/test_region_edits.py` (report wording).

## Built

As planned. See the tests above.
