# Plan: benchmark reliability fixes (ailabel, agentic-tictactoe, kaizen-agent)

Approved 2026-10-31 (no amendments).

## Request

Fixes from the first google-genai benchmark runs (~/apifix-runs/*-genai-1):

1. ailabel's baseline install timed out at 900 s because pulling the
   python:3.13-slim image happened inside the install phase. Pull the
   image before the install phase, outside its time limit, and report
   the pull time separately.
2. agentic-tictactoe's baseline install failed twice with pip
   ReadTimeoutError from files.pythonhosted.org (slow network). Give pip
   a longer --timeout and more --retries, and retry an install that
   failed on a network error (ReadTimeoutError, connection reset) up to
   3 times in all. Report the retries.
3. kaizen-agent aborted on HTTP 522 from the hosted endpoint after 5
   attempts. Treat 502/503/504/520-524 like a network outage (the
   existing 30/60/120 s waits), not as fatal.

Tests for each. Run the full suite once, then ruff. Commit without
Co-Authored-By lines and push to origin main.

## Plan

1. **Image pull (`sandbox.py`, `docker.py`):** add `DockerClient.pull(image,
   timeout_s)`, implemented in `DockerCLI` by factoring `run()`'s subprocess
   handling into a shared `_exec()`. `Sandbox._install()` pulls the image
   once before the install-attempt loop (and again if it switches to the
   non-slim image on a build error), with its own `SandboxConfig
   .image_pull_timeout_s` (separate from `install_timeout_s`). A failed or
   timed-out pull is not fatal — it's noted, and the install phase's own
   `docker run` falls back to pulling implicitly. Pull time is reported on
   `SandboxResult.image_pull_s` and folded into `duration_s`.

2. **pip timeout/retries and more network retries (`sandbox_driver.py`,
   `sandbox.py`):** the in-container pip command gets `--timeout 60
   --retries 10` (pip's defaults are 15 s / 5 retries). At the host level,
   `Sandbox._install()`'s network-failure retry changes from a single
   bool (`install_retried`, one retry) to a bounded counter
   (`MAX_NETWORK_INSTALL_RETRIES = 3`), so a network-looking failure is
   retried up to 3 times (4 attempts total) before giving up.
   `max_install_attempts` goes from 4 to 6 so a network retry run still has
   room for an image-switch or lockfile-conflict retry in the same run. The
   note reported changes from "retried once" to "retried N time(s)" so the
   count is visible.

3. **Gateway-status outage handling (`llm_anthropic.py`,
   `llm_openai_compat.py`):** both providers already wait out a dead
   connection or a hung request mid-job with `OUTAGE_WAITS_S` (30/60/120 s),
   but only for connection-level failures. A 5xx edge/gateway status
   (502/503/504/520-524) was treated as an ordinary SDK/HTTP error and
   failed the job immediately once any short retries were spent. Both
   providers now also wait out those specific statuses like an outage
   (gated on `self.answered`, same as the existing connection-error path,
   so a dead endpoint that's never answered still fails fast). 500 and 529
   are deliberately left out, matching the request's exact status list.

## Built

- `docker.py`: `DockerClient.pull()` / `DockerCLI.pull()`, sharing `_exec()`
  with `run()`.
- `sandbox.py`: `Sandbox._pull_image()`, called before the install loop and
  on an image switch; `SandboxConfig.image_pull_timeout_s` (300 s);
  `SandboxResult.image_pull_s`; `MAX_NETWORK_INSTALL_RETRIES = 3`;
  `max_install_attempts` default 4 -> 6; retry note now counts.
- `sandbox_driver.py`: pip install command gets `--timeout 60 --retries 10`.
- `llm_anthropic.py`: `OUTAGE_STATUS_CODES`, `_is_outage_status()`, used
  alongside `_is_connection_error()` in `complete()`.
- `llm_openai_compat.py`: `OUTAGE_STATUSES`, an outage-wait branch in
  `_call()` after the short-retry (`RETRY_STATUSES`) branch.
- Tests: `test_docker_cli.py` (pull), `test_sandbox.py` (pre-pull, failed
  pull not fatal, pull on image switch, 3x network retry, retry then
  success), `test_sandbox_driver.py` (pip flags), `test_llm.py` (gateway
  status waited out / bounded / other 5xx statuses not waited out),
  `test_llm_openai_compat.py` (gateway status waited out after short
  retries, bounded, not waited out before the first answer).
- Full suite (`pytest -q --docker`): 2091 passed. `ruff check` / `ruff
  format --check`: clean.
