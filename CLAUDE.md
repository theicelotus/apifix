# apifix

apifix automatically fixes Python code broken by API/library breaking changes, verifies each fix
in a sandbox, and (later) opens a pull request with the evidence. Inspired by YC's
"Self-Maintaining APIs" request.

## Version 1 scope

- Python codebases only.
- First migration: OpenAI Python library 0.x -> 1.x (`openai-v1`), e.g.
  `openai.ChatCompletion.create` -> `client.chat.completions.create`, dict responses -> objects,
  renamed error classes.
- Input: a local repo path or GitHub URL + a migration name.
- Pipeline:
  1. **Scan** for affected call sites (Python `ast`, no LLM).
  2. **Agent** proposes a fix.
  3. **Sandbox**: run the repo's tests in Docker with no network.
  4. **Retry** with the error output, up to 3 times.
  5. **Output** a diff + verification report.

### Later (not now)

FastAPI service, Arq/Redis job queue, GitHub App, automatic change detection, TypeScript
dashboard. Do not build these until asked.

## Layout

```
src/apifix/
  cli.py              `apifix scan <path> --migration openai-v1 [--json]`
  api/                FastAPI app (later)
  worker/             background jobs (later)
  engine/
    models.py         Finding / ScanResult dataclasses
    walk.py           shared repo walker (skips venvs/tooling dirs, never follows symlinks)
    dependencies.py   parses requirements/pyproject/setup.py/setup.cfg/Pipfile
    scanner.py        finds affected call sites + dependency pins (ast, no LLM)
    migrations/       one package per migration; registry in __init__.py
      openai_v1/      rules.py (pure data tables) + migration-specific heuristics
    project.py        target repo facts: Python version, test runner, install plan, lockfile pins
    sandbox.py        two-phase Docker run (install with network, test without)
    sandbox_driver.py runs INSIDE containers (Python 3.8+ compatible, stdlib only)
    docker.py         DockerClient protocol + `docker` CLI implementation (bounded output)
    test_results.py   JUnit / unittest output -> counts + per-test outcomes
    agent.py          LLM loop: propose -> verify -> retry           (not built yet)
    verifier.py       test results; checks agent didn't weaken tests (not built yet)
benchmark/            real repos + scoring
tests/                pytest; fixtures/ holds realistic sample code
```

## Hard rules

- **Never delete, skip, or weaken tests to make them pass.** This applies to apifix's own tests
  and to anything the agent does to a target repo. The verifier must enforce it.
- **Sandboxes have no network access** and strict time/memory limits. The only networked step is
  dependency install, and it never sees the repo's code: just a generated requirements file.
  Repo code (including setup.py) and tests only ever run in the `--network none` test phase.
- **Secrets never enter the sandbox** (no API keys in env, mounts, or images).
- Small, testable steps. Every module ships with tests.

## Conventions

- The scanner is pure `ast` + stdlib parsing. No LLM, no network, no third-party runtime deps.
- Migrations are data-driven: the scanner is generic and takes a `Migration`; a migration supplies
  rule tables and heuristics. Adding a migration must not require touching the walker.
- Findings carry a `confidence`: `certain` for exact API matches, `likely` for heuristics
  (e.g. dict-style response access, or renames where 1.x split one class into several).
- Paths in findings are relative to the scan root. Output is sorted and deterministic.

## Dev commands

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                  # Docker integration tests skip if Docker is unusable
.venv/bin/pytest -q -m docker        # only the Docker integration tests (need network)
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/apifix scan tests/fixtures/openai_v0 --migration openai-v1 [--json]
```

## Known limitations (scanner)

- No cross-function tracking of response objects: dict-style access is only flagged when the
  response is assigned and used in the same function/module scope.
- No `getattr(openai, ...)` or other dynamic-attribute detection.
- `.ipynb` notebooks are not scanned yet.

## Known limitations (sandbox)

- Dependencies come from static parsing. Requirements computed dynamically in `setup.py`, `-r`
  includes outside the repo root / `requirements/`, and nested packages in monorepos are missed;
  a missing dependency shows up as an import error in the test phase.
- Local-path, editable and VCS dependencies (`-e .`, `./libs/x`, Poetry `path`/`git`) are not
  installed. The repo itself is installed offline in the test phase (`pip install --no-deps
  --no-build-isolation -e`), using the build backend from `[build-system] requires`; if that
  fails (dynamic versions like setuptools_scm without `.git`, missing build deps) the repo is put
  on `PYTHONPATH` instead (`install_method` records which).
- `.git` is not copied into the sandbox (it can hold credentials), so tests or builds that need
  git metadata fail or fall back to `PYTHONPATH`.
- Lockfiles (`poetry.lock`, `Pipfile.lock`, fully pinned requirements files) are applied as pip
  constraints, not exact environments: only pins of packages that get installed apply. Pins that
  contradict a declared requirement (e.g. after a fix bumped a version) are dropped, and if pip
  still cannot resolve, the install is retried once without the lockfile (`lockfile_applied`).
  `uv.lock` is not read yet.
- Build-error detection (to retry on the full `python:<ver>` image) is string matching on pip
  output; the full image also has more system libraries but not all of them.
- Python version: CPython 3.8-3.14 only; CI detection is GitHub Actions only (regex, no YAML parser).
- The test phase's overlay venv reaches dependencies through a `.pth` file, so console scripts
  installed into `/deps/venv/bin` run against the deps venv, not the overlay (no editable repo).
- Containers run as the host UID (or nobody if the host user is root). Rootless Docker /
  user-namespace remapping is not handled. Bind mounts use `:z` for SELinux; only temp dirs that
  apifix created are ever mounted.
- Third-party sdists still execute their own build scripts during the networked install phase
  (that is what installing them means). The repo's own code never does.
- Each run reinstalls dependencies from scratch (no caching between runs yet).
