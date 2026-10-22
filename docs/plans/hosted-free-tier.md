# Hosted free tier for day-to-day testing

## Request

Use a hosted free tier (OpenAI-compatible, https://api.example.com/openai/v1) for fast testing
instead of the local 7B on CPU:

- Explain how apifix takes an API key for a hosted openai-compat endpoint; if there is no
  clean way, add one (env var + `--api-key-env NAME`). The key must never appear in the
  trajectory, report, logs or error messages.
- Simplest correct way to run the hosted provider at a price of 0 (`--prices` / `$APIFIX_PRICES`), in the
  README.
- Rate limits (~30 requests/min, per-minute token limits): handle HTTP 429 properly (wait for
  Retry-After or back off, retry, not a model failure), clear message when the daily limit is
  hit. Tests.
- README: "Fast testing with a hosted free tier", with the training-data note.
- Exact command for resume-analyzer in whole-file mode; which the hosted provider model to use.
- Full suite with `--docker` and ruff, then commit. No live tests.

## Plan

Already in place: `APIFIX_LLM_API_KEY` or `--api-key-env NAME` (never `OPENAI_API_KEY`), only
sent over https to remote hosts; no new variable needed.

1. `HTTPResponse` carries response headers (lower-cased); `urllib_transport` fills them.
2. HTTP 429 (requests that allow retries): wait as the server says (`Retry-After` seconds or
   HTTP date, `retry-after-ms`, or "try again in 1m2.5s" in the message), else exponential
   backoff (2, 4, 8, 16, 32, 60 s). Up to 6 waits per request, each at most 120 s; the waits
   happen inside the provider, so the agent sees one slow request, not a failure. A note
   records how often and how long (`rate limited N time(s) ...`).
3. Daily / quota limits (`per day`, `RPD`, `TPD`, `daily`, `insufficient_quota`, `exceeded
   your current quota`), a requested wait over 120 s, or 429s past the retry budget: stop with
   a clear error (what limit, how long the server says to wait, what to do).
4. HTTP 413 / "request too large": clear error naming the per-minute token limit, the
   request's reply-token reservation, and the options to lower.
5. Server error messages are scrubbed of the API key before they reach errors (servers that
   echo the key).
6. CLI `--max-output-tokens N` (`AgentConfig.max_output_tokens`, default 16,000): free tiers
   count a request's reply reservation against their per-minute token limit.
7. README section for the hosted provider: key, zero-price TOML via `$APIFIX_PRICES`, command, model
   listing, rate limits, training-data note. CLAUDE.md known limitations.

## Approval

The request asked for the change, the suite run and the commit directly; no amendments.

## Status

Committed with the full `pytest -q --docker` suite not green because of slow PyPI downloads
(3 dependency installs in `tests/integration/test_verifier_docker.py` hit the 600 s limit; one
smoke-check test failed once and passed alone). The Docker suite still needs a clean rerun
when the network is faster.

## Later change: User-Agent and blocked 403s

Request: preflight against the hosted provider failed with "HTTP 403 ... (no error message) (check the API
key)" although the key worked with curl; curl with `-A "Python-urllib/3.12"` also got 403, so
the server's firewall blocks urllib's default User-Agent. Send `User-Agent: apifix/<version>`
and `Accept: application/json` on every openai-compat request (chat, model list, `/api/ps`);
when a hosted endpoint returns 403 with an empty or non-JSON body, say the request was
probably blocked before reaching the API (firewall, region restriction, blocked client)
instead of suggesting the key. Tests for the header and the message; fast suite and ruff only
(the change does not touch the sandbox).
