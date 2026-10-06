# Plan: v1 bootstrap and OpenAI 0.x scanner

Approved 2026-10-06. Recovered from the session transcript; the request and amendments are verbatim.

## Request

I'm building "apifix", a tool that automatically fixes Python code broken by
API/library breaking changes, verifies each fix in a sandbox, and (later)
opens a pull request with the evidence. Inspired by YC's "Self-Maintaining
APIs" request.

Version 1 scope:
- Python codebases only
- First migration: OpenAI Python library 0.x -> 1.x
  (e.g. openai.ChatCompletion.create -> client.chat.completions.create,
  dict responses -> objects, renamed error classes)
- Input: a local repo path or GitHub URL + a migration name
- Pipeline: scan for affected call sites (Python ast, no LLM) -> agent
  proposes fix -> run repo's tests in a Docker sandbox with no network ->
  retry with the error up to 3 times -> output diff + verification report
- Later (not now): FastAPI service, Arq/Redis job queue, GitHub App,
  automatic change detection, TypeScript dashboard

Planned structure:
apifix/
  api/          FastAPI app
  worker/       background jobs
  engine/
    scanner.py    finds affected call sites (ast)
    migrations/   one folder per migration
    agent.py      LLM loop: propose -> verify -> retry
    sandbox.py    Docker: clone, install, run tests, no network
    verifier.py   test results; checks the agent didn't weaken tests
  benchmark/    real repos + scoring
  tests/

Rules:
- Never delete, skip, or weaken tests to make them pass
- Sandboxes have no network access and strict time/memory limits
- Secrets never enter the sandbox
- Small, testable steps; write tests with every module

Tasks for now:
1. Create CLAUDE.md capturing all of the above
2. Set up the project skeleton (pyproject.toml, folders, pytest)
3. Build engine/scanner.py: find every old-style OpenAI 0.x usage in a
   repo and report file, line, the old pattern, and the change needed.
   Include tests with realistic sample code.
Explain your plan before writing code.

## Plan

The plan below already has the amendments folded in (it was revised before approval).

### Context
Empty git repo (`master`, no commits). Python 3.13, pip, Docker 29 available (no uv/poetry).
Goal: lay the foundation for apifix (auto-fix code broken by library breaking changes) and build
the first real component — a deterministic, AST-only scanner that finds every OpenAI 0.x usage and
says what must change. Its output becomes the agent's work list later, so it must be precise,
structured, and migration-agnostic.

### 1. CLAUDE.md
Sections: product summary; v1 scope (Python only, openai 0.x→1.x, input = path/GitHub URL + migration
name, pipeline scan→propose→sandbox test→retry ≤3→diff+report); "Later, not now" list (FastAPI,
Arq/Redis, GitHub App, change detection, TS dashboard); planned layout; **hard rules** (never
delete/skip/weaken tests; sandbox no network + time/memory limits; secrets never enter sandbox;
small testable steps, tests with every module); dev commands (venv, `pytest`, `ruff`); conventions
(scanner is pure ast, no LLM; migrations are data-driven, one package per migration).
**Known limitations** section: no cross-function tracking of response objects; no
`getattr(openai, ...)` / dynamic-attribute detection; `.ipynb` notebooks not scanned yet.

### 2. Skeleton
```
pyproject.toml         hatchling, requires-python >=3.11, no runtime deps;
                       [dev] extra: pytest, pytest-cov, ruff; script `apifix = apifix.cli:main`
.gitignore             .venv, __pycache__, .pytest_cache, dist
README.md              short
src/apifix/__init__.py
src/apifix/cli.py               `apifix scan <path> --migration openai-v1 [--json]`
src/apifix/engine/__init__.py
src/apifix/engine/models.py     Finding, ScanResult dataclasses
src/apifix/engine/scanner.py
src/apifix/engine/migrations/__init__.py        registry: get_migration(name)
src/apifix/engine/migrations/openai_v1/__init__.py
src/apifix/engine/migrations/openai_v1/rules.py  rename/removal tables (pure data)
src/apifix/api/__init__.py, worker/__init__.py   placeholders only (empty, docstring "later")
benchmark/README.md                               placeholder
tests/  conftest.py, fixtures/openai_v0/*.py, test_scanner.py, test_cli.py
```
(src layout so tests import the installed package, not cwd.) agent.py / sandbox.py / verifier.py
are NOT created yet — they come in later steps with their own tests.

### 3. Scanner design (`engine/scanner.py`)
**Data model** (`models.py`):
`Finding(file, line, col, end_line, end_col, category, old_pattern, suggestion, confidence)`
— `category` ∈ {import, client_config, api_call, error_class, response_access, removed_module,
dependency};
`confidence` ∈ {certain, likely} (dict-access findings are heuristic → `likely`).
`ScanResult(findings, files_scanned, parse_errors)`.

**Walk**: iterate `*.py` under root, skipping `.git`, `.venv`/`venv`, `site-packages`,
`node_modules`, `__pycache__`, `build`, `dist`. Never follow symlinks (files or dirs). Parse with
`ast.parse`; on `SyntaxError` or `UnicodeDecodeError` record in `parse_errors` and continue.
Accept a single file too.

**Dependency files** (category `dependency`, separate light parser, no ast): `requirements*.txt`,
`pyproject.toml` (tomllib: `project.dependencies`, optional-deps, poetry deps), `setup.cfg`
(configparser `install_requires`), `setup.py` (ast: string literals in `install_requires=`),
`Pipfile` (tomllib). Flag an `openai` spec whose allowed range is entirely `< 1.0` (e.g. `==0.28.1`,
`<1`, `~=0.27`, poetry `^0.28`) → suggest `openai>=1.0`. Unpinned `openai` is not flagged. Sort findings by (file, line, col).

**Per-file `ast.NodeVisitor`** with import-alias resolution so we catch all spellings:
- Track aliases: `import openai`, `import openai as oa`, `from openai import ChatCompletion [as CC]`,
  `import openai.error`, `from openai.error import RateLimitError`, `from openai import error`.
  Resolve any `Attribute`/`Name` chain to a canonical dotted name (e.g. `oa.ChatCompletion.create`
  → `openai.ChatCompletion.create`). Only report names that resolve to `openai.*` — a local class
  called `ChatCompletion` is not flagged.
- **API resources** (`rules.py` table, canonical → suggestion):
  ChatCompletion.create/acreate → `client.chat.completions.create` (/`AsyncOpenAI`),
  Completion → `client.completions.create`, Embedding → `client.embeddings.create`,
  Image.create/create_edit/create_variation → `client.images.generate/edit/create_variation`,
  Moderation → `client.moderations.create`, Audio.transcribe/translate →
  `client.audio.transcriptions/translations.create`, File → `client.files.*`,
  FineTuningJob → `client.fine_tuning.jobs.*`, Model.list/retrieve → `client.models.*`.
  Removed: Edit/Engine/Customer, and legacy **FineTune** → "removed; migrate manually to
  client.fine_tuning.jobs (different params)". Also flags kwargs that changed: `engine=`/`deployment_id=` → `model=`,
  `request_timeout=` → `timeout=`, `api_key=`/`organization=` per-call → set on client.
- **Module-level config**: assignments to `openai.api_key|api_base|api_type|api_version|
  organization|proxy|requestssession|aiosession` → construct `OpenAI(api_key=..., base_url=...)`.
  If the file sets `api_type = "azure"`, config findings instead suggest
  `AzureOpenAI(azure_endpoint=..., api_version=..., api_key=...)` (api_base → azure_endpoint).
- **Error classes** (`openai.error.X` used anywhere: `except`, `isinstance`, raise, import):
  InvalidRequestError→BadRequestError with confidence `likely` and note "1.x splits this by status
  (NotFoundError, UnprocessableEntityError, ConflictError…); except blocks may need several
  classes", Timeout→APITimeoutError,
  ServiceUnavailableError→InternalServerError, PermissionError→PermissionDeniedError,
  RateLimitError/APIError/AuthenticationError/APIConnectionError → same name at `openai.`,
  TryAgain/SignatureVerificationError → removed/no equivalent. `openai.error` module → `openai`.
- **Removed modules**: `openai.embeddings_utils`, `openai.util`, `openai.cli`, `openai.wandb_logger`.
- **Response dict access (heuristic)**: within a function/module scope, track names assigned from a
  flagged API call (incl. `await`). Flag `x["choices"]`, `x.get(...)`, `x["data"]`, `x["usage"]`,
  `.to_dict()`-free json use, and chained subscripts → "use attribute access (`x.choices[0]
  .message.content`) or `x.model_dump()`", confidence `likely`. Also flag stream chunks
  (`for chunk in resp: chunk["choices"][0]["delta"]`) when `resp` came from a `stream=True` call.

Scanner is generic: it takes a `Migration` object (rules from `rules.py`); openai_v1 just supplies
tables + the response-access heuristic, so later migrations plug in without touching the walker.

### 4. Tests (`tests/`)
Realistic fixture files (not toy one-liners), e.g.:
- `chatbot.py` — `openai.api_key = os.getenv(...)`, ChatCompletion.create, `resp["choices"][0]
  ["message"]["content"]`, `except openai.error.RateLimitError`.
- `async_streaming.py` — `await openai.ChatCompletion.acreate(stream=True)` + chunk dict access.
- `embeddings_search.py` — `from openai.embeddings_utils import cosine_similarity`, Embedding.create
  with `engine=`.
- `azure_config.py` — api_type/api_base/api_version + `deployment_id=`.
- `aliased_imports.py` — `import openai as oai`, `from openai import ChatCompletion as CC`,
  `from openai.error import InvalidRequestError, Timeout`.
- `already_v1.py` — modern `OpenAI()` client code → **zero findings** (false-positive guard).
- `unrelated.py` — local `ChatCompletion` class, unrelated `["choices"]` dict → zero findings.
- `broken_syntax.py` — recorded in parse_errors, scan continues.
- `legacy_finetune.py` — `openai.FineTune.create` → removed; `openai.FineTuningJob.create` → rename.
- Dependency fixtures (tmp_path-built): requirements.txt `openai==0.28.1`, pyproject `openai<1`,
  setup.py `install_requires=["openai~=0.27"]`, Pipfile; plus `openai>=1.3` → not flagged.
- Non-UTF-8 file and a symlink loop / symlinked dir (built in tmp_path) → error recorded, no hang,
  symlink target not scanned.

Test cases assert exact (line, category, suggestion-substring) per fixture; directory walk skips
`.venv`; findings sorted/deterministic; CLI text and `--json` output; migration registry rejects
unknown names.

### Verification
```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/apifix scan tests/fixtures/openai_v0 --migration openai-v1
.venv/bin/apifix scan tests/fixtures/openai_v0 --migration openai-v1 --json | python3 -m json.tool
```
No commits unless you ask.

## Amendments (approval)

Plan approved with these amendments:
1. Add a "dependency" finding category: scan requirements*.txt, pyproject.toml,
   setup.py, setup.cfg, Pipfile for openai pins < 1.0 and suggest openai>=1.0.
   Add a fixture and test for it.
2. FineTune (legacy) is removed, not a rename. Report it as removed with a
   "migrate manually to client.fine_tuning.jobs (different params)" note.
   Only FineTuningJob maps directly to client.fine_tuning.jobs.
3. InvalidRequestError -> BadRequestError should be confidence "likely", with a
   suggestion noting 1.x splits it by status (NotFoundError etc.), so except
   blocks may need several classes.
4. For api_type azure, suggest AzureOpenAI(azure_endpoint=..., api_version=...),
   not base_url.
5. Record files that fail with UnicodeDecodeError in parse_errors and continue;
   don't follow symlinks when walking.
6. Add a "Known limitations" section to CLAUDE.md: no cross-function tracking of
   response objects, no getattr(openai, ...) detection, .ipynb not scanned yet.
Then proceed.
