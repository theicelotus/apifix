# Plan: engine/sandbox.py

Approved 2026-10-06. Recovered from the session transcript; the request and amendments are verbatim.

## Request

Next: engine/sandbox.py. Requirements:
- Two phases in Docker: (1) install phase with network to install the repo's
  dependencies; (2) test phase with --network none. Never run the repo's tests
  or any modified code while network is enabled.
- Detect the repo's Python version where possible (pyproject, setup.py,
  .python-version, CI config); default to 3.11. Detect the test command
  (pytest / unittest); if no tests exist, return status "unverified", not "passed".
- Limits: CPU, memory, wall-clock timeout per phase; run as non-root user;
  containers always removed afterwards, even on errors/timeouts.
- No secrets or host env vars passed into containers.
- Return a structured result: phase statuses, exit codes, test counts
  (passed/failed/skipped/errors), truncated stdout/stderr, duration.
- Record test counts so the verifier can later detect tests that were
  deleted or skipped after a fix.
- Unit tests that don't need Docker (mock the Docker layer), plus a small set
  of integration tests marked so they only run when Docker is available.
Explain the plan before writing code.

## Plan

### Main design decision: the install phase never sees the repo's code
Running `pip install -e .` or `-r requirements.txt` with the network on would execute the repo's `setup.py` while connected, which breaks your rule. So the two phases get different inputs:

- **Install phase (network on):** the container sees only a generated `requirements.apifix.txt`. On the host, I build it from the repo's dependency files using the existing `dependencies.py` parser. Anything that points at the repo's own code is dropped (`-e .`, local paths, `file:`). Optional/test extras and dependency groups are included, plus `pytest` when that's the runner. pip installs everything into a venv in a host temp dir mounted at `/deps`.
- **Test phase (`--network none`):** a fresh container mounts `/deps` read-only and a copy of the repo (not the original) at `/work`. The repo is imported via `PYTHONPATH=/work/src:/work` rather than installed, so its `setup.py` is never executed.

**Tradeoff:** dependencies that only exist dynamically in `setup.py` (computed rather than literal strings) are missed. A missing dependency shows up as a test-phase import error, so it fails loudly. I'll list this under Known limitations.

### Files
| File | Contents |
|---|---|
| `engine/project.py` | Detects the Python version, the test runner and whether test files exist. Pure functions, reusable by the agent later. |
| `engine/test_results.py` | Parses pytest JUnit XML and `unittest -v` output into counts and per-test outcomes. The verifier will reuse it. |
| `engine/sandbox.py` | `run_sandbox(repo, config, docker=None) -> SandboxResult`: builds the container commands, runs both phases, cleans up. |
| `engine/docker.py` | A small `DockerClient` protocol plus a `DockerCLI` that calls the `docker` binary via subprocess. No new runtime dependency, and easy to fake in tests. |

**Python version detection, in priority order:**
1. `.python-version`
2. `runtime.txt`
3. `python-version:` in `.github/workflows/*.yml` (regex, no PyYAML; highest version listed)
4. `requires-python` / Poetry `python` / `python_requires` in setup.py or setup.cfg (picks 3.11 if allowed, otherwise the highest supported version that fits)
5. Default 3.11

Explicit picks from steps 1–3 must satisfy the constraint from step 4. The result records which source was used.

**Test runner detection:**
- **pytest** if there's any sign of it: a dependency on it, `[tool.pytest.ini_options]`, `pytest.ini`, `conftest.py`, `[tool:pytest]`, or `tox.ini [pytest]`.
- **unittest** if the test files only import `unittest`.
- **No tests:** if there are no `test_*.py` / `*_test.py` files, no container runs and the status is `unverified`.

### Containers
- **Both phases:**
  - Runs as a non-root user: the host UID, or 65534 if the host user is root.
  - `--read-only` root filesystem, `--tmpfs /tmp`, `HOME=/tmp`.
  - `--cap-drop ALL`, `--security-opt no-new-privileges`.
  - `--cpus`, `--memory` with `--memory-swap` set equal, `--pids-limit`.
  - Image is `python:<ver>-slim`; containers are labeled `apifix=1`.
- **Environment:** the only variables passed are a hardcoded allowlist with literal values (`HOME`, `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE`, `PIP_NO_INPUT`, …). Nothing comes from `os.environ`. The `docker` CLI process itself also runs with a minimal environment.
- **Wall-clock timeout per phase:** the subprocess times out, then the container is force-removed with `docker rm -f`.
- **Cleanup:** every container gets a unique name (`apifix-<uuid>`). The containers and the host temp dirs (deps, work copy, results) are removed in a `finally` block, so this also happens on errors, timeouts and Ctrl-C. I won't rely on `--rm`, because a killed `docker run` client leaves its container running.
- **pytest flags:** `--junitxml=/results/junit.xml -p no:cacheprovider`. Exit code 5 (no tests collected) maps to `unverified`.

### Result
```
SandboxResult:
  status      passed | failed | unverified | error | timeout
  reason
  python_version, python_version_source
  runner, test_command
  install / test: PhaseResult(status ok|failed|timeout|skipped|error,
                              exit_code, stdout, stderr, duration_s)
  counts:     TestCounts(total, passed, failed, skipped, errors)
  tests:      [TestCaseResult(id, outcome)]
  duration_s
```
- **When it counts as `unverified`:** no tests exist, none were collected, or every test was skipped. A run where nothing actually passed is never reported as `passed`.
- **Per-test IDs:** these go beyond the counts you asked for. With them, the verifier can name the specific test that was deleted or newly skipped after a fix, instead of only noticing that a number changed.
- **Output truncation:** stdout/stderr keep the first ~4 KB and the last ~16 KB, with a marker in between, so pytest's failure summary at the end is kept.

### Tests
- **Unit tests (no Docker), using a fake `DockerClient` that records calls:**
  - The test-phase command always includes `--network none`.
  - The install phase never mounts the repo.
  - No host environment value appears in any command (I'll set a canary env var to check).
  - The user is non-root and all limits are present.
  - Cleanup runs on success, failure, timeout and a raised exception.
  - A failed install means the test phase is skipped and the status is `error`.
  - A repo with no tests means no Docker calls at all.
  - The parsers, version detection, runner detection and requirements generation each get table-driven cases.
- **Integration tests (`@pytest.mark.docker`):** these skip automatically unless `docker info` succeeds. They need network for the image pull and the pip install. They cover tiny repos for:
  - a passing test
  - a failing test
  - a test that tries to open a socket (must fail inside the sandbox)
  - a `sleep` that hits the timeout (the container must be gone afterwards)
  - checks that the test runs as a non-root UID and can't see a host env var

### Out of scope for this step
Cloning GitHub URLs (that comes with the pipeline/CLI step), and caching installed dependencies between runs.

One question: should I rename `SandboxResult` statuses or keep `timeout` separate from `error`? I'd keep them separate, because the agent's retry logic should treat a timeout differently from a broken install. Tell me to go ahead and I'll build it.

## Amendments (approval)

Approved, with amendments. Keep timeout separate from error.
1. In the test phase (network already off), try
   `pip install --no-deps --no-build-isolation -e /work` before running tests,
   so the repo is installed without network. Add the repo's [build-system]
   requires (setuptools, hatchling, etc.) to requirements.apifix.txt. If the
   offline install fails, fall back to the PYTHONPATH approach. Record which
   method was used in SandboxResult.
2. If a lockfile exists (poetry.lock, Pipfile.lock, fully pinned
   requirements), install from its exact versions.
3. If the install phase fails on python:<ver>-slim with a build error, retry
   once with python:<ver>; record the image used.
4. Confirm /work (the repo copy) is writable and /deps stays read-only.
Add any new tradeoffs to Known limitations in CLAUDE.md. Then build it.

## Later changes

- setuptools-scm projects get `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0` in the test phase (commit 315b2f8).
- Docker integration tests are opt-in (`pytest --docker` / `-m docker`) (commit 4c9e402).
