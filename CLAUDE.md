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
    scanner.py        finds affected call sites + dependency pins (ast, no LLM)
    migrations/       one package per migration; registry in __init__.py
      openai_v1/      rules.py (pure data tables) + migration-specific heuristics
    agent.py          LLM loop: propose -> verify -> retry           (not built yet)
    sandbox.py        Docker: clone, install, run tests, no network  (not built yet)
    verifier.py       test results; checks agent didn't weaken tests (not built yet)
benchmark/            real repos + scoring
tests/                pytest; fixtures/ holds realistic sample code
```

## Hard rules

- **Never delete, skip, or weaken tests to make them pass.** This applies to apifix's own tests
  and to anything the agent does to a target repo. The verifier must enforce it.
- **Sandboxes have no network access** and strict time/memory limits.
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
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/apifix scan tests/fixtures/openai_v0 --migration openai-v1 [--json]
```

## Known limitations (scanner)

- No cross-function tracking of response objects: dict-style access is only flagged when the
  response is assigned and used in the same function/module scope.
- No `getattr(openai, ...)` or other dynamic-attribute detection.
- `.ipynb` notebooks are not scanned yet.
