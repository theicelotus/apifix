# Plan: second migration `google-genai` (google-generativeai -> Google Gen AI SDK)

Status: approved with amendments (2026-10-30); engine changes E1-E4 built. Next: E5-E6 and
the migration package (section 7, steps 1-6).

## Request

Plan only (docs/plans/google-genai.md, request verbatim), then stop for
my approval.

Second migration: `google-genai` — code using the deprecated
google-generativeai SDK (support ended 2025-11-30, repo archived) moved
to the Google Gen AI SDK (`google-genai`, `from google import genai`).

Before planning, check the real APIs: pip download --no-deps the last
google-generativeai release and the current google-genai release into
your scratchpad and read their public API (configure/GenerativeModel/
generate_content/start_chat/embed_content/count_tokens, response
objects, streaming, async, safety settings, errors). Quote what you rely on.

Plan, following how openai-v1 is built (data-driven migration package):
- scanner findings and their confidence levels; the dependency pin
  (google-genai version range) and removing google-generativeai;
- the guide parts for the model (configure -> genai.Client(api_key=...),
  GenerativeModel(...).generate_content -> client.models.generate_content
  (model=..., contents=..., config=...), chat, embeddings, response
  access, streaming, async via client.aio);
- migration-specific checks (like the openai config and streaming
  checks): API key handling (GOOGLE_API_KEY / GEMINI_API_KEY), test
  mocks/patch targets;
- what is out of scope at first (e.g. Vertex AI mode, files API);
- fixtures and tests, and 3-5 real repos to benchmark on from
  ~/apifix-targets/short-google-genai.tsv (check each still uses the
  old SDK on its default branch);
- open questions for me.

## 1. The real APIs

Downloaded with `pip download --no-deps`: **google-generativeai 0.8.6** (the last release,
`Requires-Python >=3.9`) and **google-genai 2.34.0** (current, 2026-10-29, `Requires-Python
>=3.11`). Release metadata and the upstream CHANGELOG were checked for the version range.

### Old: google-generativeai 0.8.6

Importing it warns: `"All support for the google.generativeai package has ended ... Please
switch to the google.genai package"` (`FutureWarning`, `__init__.py`).

Public names (`google/generativeai/__init__.py`): `configure`, `GenerativeModel`, `ChatSession`,
`embed_content`, `embed_content_async`, `upload_file` / `get_file` / `list_files` /
`delete_file`, `list_models` / `get_model` / `get_base_model`, tuned-model functions,
`list_operations` / `get_operation`, `GenerationConfig`, and modules `caching`, `protos`,
`types`.

- **configure** (`client.py:302`): `configure(*, api_key=None, credentials=None,
  transport=None, client_options=None, client_info=None, default_metadata=())`. Key lookup when
  none is passed (`client.py:189-196`):
  ```python
  api_key = os.getenv("GEMINI_API_KEY")
  if not api_key:
      api_key = os.getenv("GOOGLE_API_KEY")
  ```
  So the old SDK prefers **GEMINI_API_KEY**. `transport` is one of `rest`, `grpc`,
  `grpc_asyncio`.
- **GenerativeModel** (`generative_models.py:73`): `GenerativeModel(model_name =
  "gemini-1.5-flash-002", safety_settings=None, generation_config=None, tools=None,
  tool_config=None, system_instruction=None)`; a name without `/` gets a `models/` prefix.
  Constructing one needs no key: the key is only needed at call time.
- **generate_content** (`:237`): `generate_content(contents, *, generation_config=None,
  safety_settings=None, stream=False, tools=None, tool_config=None, request_options=None) ->
  GenerateContentResponse`. Async: `generate_content_async` (`:344`), same arguments.
- **count_tokens** (`:399`): `count_tokens(contents=None, *, generation_config=None,
  safety_settings=None, tools=None, tool_config=None, request_options=None) ->
  protos.CountTokensResponse` (`.total_tokens`). Async: `count_tokens_async` (`:426`).
- **start_chat** (`:455`): `start_chat(*, history=None, enable_automatic_function_calling=False)
  -> ChatSession`. `ChatSession.send_message(content, *, generation_config=None,
  safety_settings=None, stream=False, tools=None, tool_config=None, request_options=None)`
  (`:512`), `send_message_async` (`:671`), the `history` property (`:802`), `rewind()`,
  `last`.
- **embed_content** (`embedding.py`): `embed_content(model, content, task_type=None,
  title=None, output_dimensionality=None, client=None, request_options=None) ->
  EmbeddingDict`. It returns a **dict**: `{"embedding": [floats]}` for one content, a list of
  lists for a batch (`embedding.py:185-219`). `embed_content_async` too.
- **Responses** (`types/generation_types.py`): `GenerateContentResponse` with `candidates`,
  `parts`, `text`, `prompt_feedback`, `usage_metadata` (`prompt_token_count`,
  `candidates_token_count`, `total_token_count`), `to_dict()`. `.text` **raises ValueError**
  when there is no valid part (`:457-480`: "The `response.text` quick accessor requires the
  response to contain a valid `Part`, but none were returned"). With `stream=True` the response
  is iterated (`__iter__`, `:650`) and `resolve()` (`:687`) finishes it;
  `AsyncGenerateContentResponse` for async.
- **Exceptions**: `BlockedPromptException`, `StopCandidateException`,
  `IncompleteIterationError`, `BrokenResponseError` (`generation_types.py:64-76`);
  `send_message` raises `BlockedPromptException(response.prompt_feedback)` (`:608`). Transport
  errors are **`google.api_core.exceptions.*`** (e.g. `except
  google.api_core.exceptions.InvalidArgument` at `generative_models.py:336`); google-api-core
  comes in as a dependency (`Requires-Dist: google-api-core`, plus
  `google-ai-generativelanguage==0.6.15`, `protobuf`, ...).
- **Safety settings** (`types/safety_types.py`): `HarmCategory`, `HarmBlockThreshold`,
  `HarmProbability` (proto enums). `to_easy_safety_dict` accepts a dict `{category:
  threshold}`, a list of dicts, and string shorthands (`"harassment"`, `"block_none"`).
- `GenerationConfig` (`generation_types.py:93`): `candidate_count`, `stop_sequences`,
  `max_output_tokens`, `temperature`, `top_p`, `top_k`, `response_mime_type`,
  `response_schema`, ...

### New: google-genai 2.34.0

Public names (`google/genai/__init__.py`): `Client`, `types`, `version`, and lazily `agents`,
`credentials`, `environments`, `interactions`, `triggers`, `webhooks`. Errors are in
`google.genai.errors`.

- **Client** (`client.py:358`): `Client(*, enterprise=None, vertexai=None, api_key=None,
  credentials=None, project=None, location=None, debug_config=None, http_options=None)`. With
  no key it reads the environment (`_api_client.py:162-174`), preferring **GOOGLE_API_KEY**:
  ```python
  env_google_api_key = os.environ.get("GOOGLE_API_KEY", None)
  env_gemini_api_key = os.environ.get("GEMINI_API_KEY", None)
  if env_google_api_key and env_gemini_api_key:
      logger.warning("Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using GOOGLE_API_KEY.")
  return env_google_api_key or env_gemini_api_key or None
  ```
  With neither set, **construction raises** (`_api_client.py:1047`): `ValueError('No API key was
  provided. Please pass a valid API key. ...')`. `GOOGLE_GENAI_USE_VERTEXAI` /
  `GOOGLE_CLOUD_PROJECT` switch to Vertex (`:872`, `:929`). Resources are **properties**
  (`client.py:584-594`: `@property def chats`, `aio`, `models`, ...), the same autospec trap
  openai 1.x has. `Client` is also a context manager (`close`, `__enter__`, `__exit__`).
- **models** (`models.py`), keyword-only:
  `generate_content(*, model, contents, config=None) -> GenerateContentResponse` (`:6241`),
  `generate_content_stream(*, model, contents, config=None) -> Iterator[...]` (`:6420`),
  `embed_content(*, model, contents, config=None) -> EmbedContentResponse` (`:6123`),
  `count_tokens(*, model, contents, config=None) -> CountTokensResponse` (`:5812`),
  `list(*, config=None) -> Pager[Model]` (`:6924`).
- **async** (`client.aio.models`): the same methods as `async def`. The async stream is
  `async def generate_content_stream(...) -> AsyncIterator[...]` (`models.py:8683`), so it needs
  **`async for chunk in await client.aio.models.generate_content_stream(...)`**.
- **chats** (`chats.py`): `client.chats.create(*, model, config=None, history=None) -> Chat`
  (`:590`); `Chat.send_message(message, config=None)` (`:221`), `send_message_stream` (`:393`),
  `get_history(curated=False)` (`:169`). Async: `client.aio.chats.create(...)`, `async def
  send_message` / `send_message_stream` (`:633`, `:888`).
- **config** (`types.GenerateContentConfig`, `types.py:6562`): `system_instruction`,
  `temperature`, `top_p`, `top_k`, `candidate_count`, `max_output_tokens`, `stop_sequences`,
  `response_mime_type`, `response_schema`, `safety_settings: list[SafetySetting]`, `tools`,
  `tool_config`, `cached_content`, `automatic_function_calling`, `thinking_config`, ... A plain
  dict is accepted everywhere a config is (`GenerateContentConfigOrDict`).
  `types.EmbedContentConfig` (`:9134`): `task_type`, `title`, `output_dimensionality`, ...
- **Responses**: `GenerateContentResponse` (`types.py:8771`) is a pydantic model with
  `candidates`, `prompt_feedback`, `usage_metadata` (same field names: `prompt_token_count`,
  `candidates_token_count`, `total_token_count`), `parsed`, ...; `.text -> Optional[str]`
  (`:8884`) **returns None** instead of raising when there is no text (`_get_text`, `:8826`).
  `EmbedContentResponse.embeddings: list[ContentEmbedding]`, each with `.values`
  (`:9333-9390`). `CountTokensResponse.total_tokens` (`:11901`). Models list `supported_actions`
  (`:11088`) where the old ones had `supported_generation_methods`.
- **Safety**: `types.SafetySetting(category=..., threshold=...)` (`:6242`), `types.HarmCategory`
  (`:425`, `HARM_CATEGORY_HARASSMENT`, ...), `types.HarmBlockThreshold` (`:471`, `BLOCK_NONE`,
  `BLOCK_ONLY_HIGH`, `OFF`, ...). The enums are case-insensitive (`_common.py:700-705`, `_missing_`
  tries `value.upper()`), so `"block_none"` still works but the old shorthand `"harassment"`
  does not.
- **Errors** (`errors.py`): `APIError(Exception)` (`:56`, with `code`, `status`, `message`;
  `__init__(self, code, response_json, response=None)`), `ClientError(APIError)` (4xx, `:308`),
  `ServerError(APIError)` (5xx, `:313`). Nothing in google-genai imports `google.api_core`, and
  google-api-core is not among its requirements.
- **Automatic function calling** is on by default when Python callables are passed in `tools`
  (`_extra_utils.py:455` `should_disable_afc` returns False unless disabled); the old SDK's
  default was off (`enable_automatic_function_calling=False`).

### Versions (PyPI metadata, CHANGELOG)

| google-genai | date | Requires-Python | note |
|---|---|---|---|
| 1.0.0 | 2025-02-05 | >=3.9 | first 1.x |
| 1.19.0 | 2025-06 | >=3.9 | "Support `GEMINI_API_KEY` as environment variable for setting API key" |
| 1.47.0 | 2025-10-29 | >=3.9 | last for Python 3.9 |
| 2.0.0 | 2026-05-07 | >=3.10 | "BREAKING CHANGES - Interactions Only ... `GenerateContent` usage in unaffected" |
| 2.33.0 | 2026-10-23 | >=3.10 | last for Python 3.10 |
| 2.34.0 | 2026-10-29 | >=3.11 | "Drop support for Python 3.10" |

Other breaking entries since 1.0 are experimental features (1.17/1.18) and
`Part.from_video_metadata` (1.10).

## 2. Engine changes this migration needs first

openai-v1 is mostly data-driven, but a few spots assume a single top-level module and an
unchanged distribution name. Each one gets its own commit, with openai-v1 behaviour (and its
tests) unchanged:

- **E1 dotted `api_modules`.** `tampering.py:128`, `unrelated.py:354-359,493,683`,
  `suspicious.py` (`prefixes[0] in free`) and `code_checks.py` compare `name.split(".")[0]` with
  `api_modules`. For Google that would have to be `"google"`, which would also cover
  google.protobuf, google.cloud and google.auth. Change these to a prefix match on dotted names
  (`google.genai`, `google.generativeai`), with `import google.generativeai` (binds `google`)
  and `import google.generativeai as genai` (binds `genai`) both resolved.
  `suspicious.check_dependencies` already maps `google.genai` -> `google-genai` through
  `normalize_name`.
- **E2 distribution rename in pin edits.** `pin_edits.py` rewrites only the version
  (`openai==0.28.1 -> openai>=1.55.3,<3`). Add `Migration.source_package`
  (`google-generativeai`): its entry is replaced by `google-genai<spec>` in every format
  pin_edits handles (requirements, PEP 621 / Poetry / PDM tables, setup.py/cfg, Pipfile, conda).
  When the same file already declares google-genai, the old entry is removed and the existing
  google-genai pin is raised if it is below the floor. `already_migrated.py` then means
  "google-generativeai not declared, google-genai >= first_version declared".
- **E3 `ClientKey` for two variables, and resource methods with no `resources` segment.**
  `env: str` becomes `envs: tuple[str, ...]` (any of them counts as setting the key in
  `code_checks._sets_key` and the CI-config check). `resource_method()` gets the
  `google.genai.models.Models.generate_content` shape (module = resource) next to openai's
  `openai.resources.chat.completions.Completions.create`.
- **E4 no-signal markers from the migration.** `verifier.py:55-68` hard-codes
  `"api_key client option must be set"` and `"OPENAI_API_KEY"`. Move the key markers to a
  `Migration.key_failure_markers` field (openai keeps its own; google-genai: `"No API key was
  provided"`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`).
- **E5 minimum Python.** Add `Migration.min_python` (`"3.9"`). When `detect_python_version`
  picks something older (a `.python-version` of 3.8), report it before any LLM call
  ("google-genai needs Python 3.9 or later; the repo pins 3.8") and stop, instead of letting pip
  fail in the sandbox.
- **E6 registry and docs.** Register the package in `migrations/__init__.py`; update the
  CLAUDE.md layout line and the README migration list; add a `docs/notes.md` section for the
  new migration's known limitations.

## 3. Migration package `src/apifix/engine/migrations/google_genai/`

Same files as `openai_v1/`: `__init__.py` (the `MIGRATION` object), `rules.py`,
`python_checks.py`, `repo_findings.py`, `guide.md`, `guide_parts.py`, `config_checks.py`,
`streaming.py`, plus `test_checks.py` for the patch-target check. Name `google-genai`, summary
"google-generativeai -> Google Gen AI SDK (google-genai)".

### 3.1 Scanner findings

Names are resolved through the file's imports as openai-v1 does (`import google.generativeai as
genai`, `from google import generativeai as gg`, `from google.generativeai import
GenerativeModel`, `import google.generativeai`). Instance tracking: a name or `self.<attr>`
assigned from `GenerativeModel(...)`, `model.start_chat(...)` or `genai.embed_content(...)`, in
the same scope or class. The new API's methods have the same names (`generate_content`), so a
call on an untracked receiver never counts as old code by name alone.

| category | old pattern | suggestion | confidence |
|---|---|---|---|
| import | `import google.generativeai [as x]`, `from google import generativeai`, `from google.generativeai[.types] import ...` | `from google import genai` (+ `from google.genai import types`) | certain |
| client_config | `genai.configure(api_key=..., transport=..., client_options=...)` | `client = genai.Client(api_key=...)`; keep the value; `transport` has no equivalent (HTTP only); `client_options={"api_endpoint": ...}` -> `http_options=types.HttpOptions(base_url=...)` | certain |
| api_call | `genai.GenerativeModel(name, system_instruction=, generation_config=, safety_settings=, tools=, tool_config=)` | no model object: keep the name, move every setting into `types.GenerateContentConfig` passed on each call | certain |
| api_call | `<model>.generate_content(c, ...)` on a tracked model | `client.models.generate_content(model=..., contents=c, config=...)` | certain; **likely** when the receiver is untracked but the file imports the old SDK and nothing in the file uses the new one |
| api_call | `... generate_content(..., stream=True)` | `client.models.generate_content_stream(...)` | certain (tracked) / likely |
| api_call | `generate_content_async`, `count_tokens_async`, `send_message_async`, `embed_content_async` | `await client.aio.models.<method>(...)` / `client.aio.chats.create(...)` | as above |
| api_call | `<model>.count_tokens(c)` | `client.models.count_tokens(model=..., contents=c)` | certain / likely |
| api_call | `<model>.start_chat(history=...)`; `chat.send_message(m, stream=True)` | `client.chats.create(model=..., config=..., history=...)`; `chat.send_message_stream(m)` | certain / likely |
| api_call | `genai.embed_content(model=, content=, task_type=, title=, output_dimensionality=)` | `client.models.embed_content(model=, contents=, config=types.EmbedContentConfig(task_type=...))` | certain |
| api_call | `genai.list_models()`, `genai.get_model(name)` | `client.models.list()`, `client.models.get(model=name)` | certain |
| api_call | `genai.GenerationConfig(...)` / `genai.types.GenerationConfig(...)` / a `GenerationConfigDict` | its fields go into `types.GenerateContentConfig(...)` | certain |
| api_call | safety settings in the old shapes (dict `{HarmCategory.X: HarmBlockThreshold.Y}`, shorthand strings) | `[types.SafetySetting(category="HARM_CATEGORY_X", threshold="BLOCK_Y"), ...]` | certain for a dict or a shorthand category; likely for a variable passed in |
| removed_module | `genai.protos.*`, `google.ai.generativelanguage` used for Gemini calls | `types.*` equivalents | certain |
| error_class | `genai.types.BlockedPromptException`, `StopCandidateException`, `IncompleteIterationError`, `BrokenResponseError` | gone: check `response.prompt_feedback.block_reason` / `candidates[0].finish_reason` | certain |
| error_class | `google.api_core.exceptions.<X>` in a file that imports the old SDK (or in a test of such a module) | `google.genai.errors.ClientError` (4xx; `e.code == 429` for `ResourceExhausted`), `ServerError` (5xx), `APIError` (both); google-api-core is no longer installed with the SDK | likely (other Google libraries also raise these) |
| response_access | `.text` read inside `try: ... except ValueError` (or `except Exception` whose handler talks about blocked/empty) | `.text` is None now: test for None | likely |
| response_access | `<embedding result>["embedding"]` | `result.embeddings[0].values` (batch: `[e.values for e in result.embeddings]`) | certain (tracked) / likely |
| response_access | `chat.history` | `chat.get_history()` | certain (tracked) |
| response_access | `response.resolve()`, `response.to_dict()`, `finish_reason` compared with an int, `supported_generation_methods` | removed / `model_dump()` / the enum's name / `supported_actions` | likely |
| dependency | any declaration of `google-generativeai` (every version is the old SDK) | replace with `google-genai>=1.19.0,<3` | certain |
| dependency | `google-genai` declared below 1.19.0 | raise to the pin | likely |
| dependency | `google-ai-generativelanguage` declared and imported nowhere else | remove (only the old SDK used it) | likely; flag only, see question 4 |

`new_api_uses`: `genai.Client`, `client.models.*`, `client.aio.*`, `client.chats.*`,
`google.genai.types` / `errors`, for `already_migrated.py`.

`adjust_findings` (repo-level, like openai's `repo_findings`): the `configure()` value and the
call sites are often in different files (a `config.py` sets the key, `llm.py` builds the
model), so the configure finding names the files that make the calls and says to pass the same
value into the `Client` built there.

### 3.2 Dependency pin

- `package = "google-genai"`, `source_package = "google-generativeai"`,
  `pin_spec = ">=1.19.0,<3"`, `first_version = "1.0.0"`, `target_name = "google-genai"`,
  `source_name = "google-generativeai"`.
- `pin_reasons`: `>=1.19.0`: "the first release that reads `GEMINI_API_KEY`, which the old SDK
  read first"; `<3`: "2.x kept `generate_content`'s API (2.0 broke only Interactions); a 3.0
  has not been checked".
- pip picks the release for the sandbox Python: 1.47.0 on 3.9, 2.33.0 on 3.10, the latest on
  3.11+. A lower bound of 1.19 still gives us everything the guide uses (`Client`, `aio`,
  `chats`, `generate_content_stream`, `EmbedContentConfig`, errors).
- `companion_pins`: none at first. If a fix keeps `google.api_core` imports (question 3), add
  `google-api-core` as a companion so removing google-generativeai does not drop it.
- `smoke_check`: `from google import genai; genai.Client(api_key="apifix-smoke-check-not-a-real-key")`.
  Building the client sends nothing, which the build checks in the sandbox with the network off.
- `ci_env_placeholders`: `GOOGLE_API_KEY` and `GEMINI_API_KEY` -> `apifix-placeholder-not-a-real-key`.

### 3.3 Guide parts (`guide.md`, selected per job as in openai-v1)

- **client** (always): `genai.configure(api_key=X)` -> `client = genai.Client(api_key=X)`,
  built where it is used, not at import time (it raises with no key). If the old code passed
  nothing, or `os.getenv("GOOGLE_API_KEY")`, then `genai.Client()` reads the environment
  itself. If it read `GEMINI_API_KEY` explicitly, keep passing that value explicitly: with both
  variables set the new SDK picks GOOGLE_API_KEY, and the old one picked GEMINI_API_KEY. Never
  drop a key value; change only what the migration needs (the openai wording).
- **client.async** (selector: `_async`, `await`): `client.aio.models.generate_content(...)`,
  `client.aio.chats.create(...)`; the async stream needs `await` before `async for`.
- **models** (api_call): `GenerativeModel(name, **settings).generate_content(c)` ->
  `client.models.generate_content(model=name, contents=c,
  config=types.GenerateContentConfig(system_instruction=..., temperature=..., safety_settings=[...]))`;
  a class that kept `self.model = GenerativeModel(...)` keeps `self.client` and `self.model_name`
  plus one config. `generation_config` fields and per-call overrides merge into `config`.
  `count_tokens`, `list_models` / `get_model`.
- **chat** (`start_chat`, `send_message`, `ChatSession`, `history`): `client.chats.create(model=,
  config=, history=)`, `send_message` / `send_message_stream`, `get_history()`.
- **embeddings** (`embed_content`): argument renames (`content` -> `contents`, `task_type` /
  `title` / `output_dimensionality` -> `config`); the result is an object, not a dict.
- **responses** (api_call, response_access): `.text` may be None (no ValueError); the same
  `usage_metadata` names; `candidates[0].content.parts`; `finish_reason` and `block_reason`
  are string enums; `to_dict()` -> `model_dump()`.
- **streaming** (per file, selector `stream`): `generate_content(stream=True)` ->
  `generate_content_stream`; chunks have `.text` (may be None, so `chunk.text or ""`); no
  `resolve()`.
- **safety** (selector `safety_settings`, `HarmCategory`): list of `types.SafetySetting`; keep
  every category and threshold as it was.
- **errors** (error_class): the table above, plus that google-api-core is no longer installed.
- **tests** (findings in test files): patch `pkg.mod.genai.Client` (not `genai.configure` /
  `genai.GenerativeModel`, which no longer exist on the module) and set
  `Client.return_value.models.generate_content.return_value`; no `autospec=True` on `Client`
  (its resources are properties); a mock response is `MagicMock(text="...")` or a real
  `types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model",
  parts=[types.Part(text="...")]))])`; simulate errors with `errors.ClientError(429, {"error":
  {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})`; a `sys.modules` stub of
  `google.generativeai` becomes a stub of `google.genai`; `monkeypatch.setenv("GEMINI_API_KEY",
  ...)` keeps working.
- **dependencies** (dependency): replace the distribution, never keep both.
- A note on `types`: when the file already uses the standard library's `types` (seen in a real
  test), use `genai.types` and don't import `from google.genai import types`.
  `known_imports` therefore lists `genai` -> `from google import genai` and `errors` ->
  `from google.genai import errors`, but not `types`, which `add_missing_imports` would
  otherwise bind over the standard library.

### 3.4 Migration fields (the rest of the `Migration` protocol)

- `api_modules = {"google.generativeai", "google.genai"}` (E1); `broad_exceptions = {"APIError",
  "GoogleAPIError", "GoogleAPICallError"}`; `companion_modules = {}`.
- `client_classes = {"Client"}`, `client_resources` = the properties of `Client` in 2.34.0
  (`models`, `chats`, `aio`, `files`, `caches`, `batches`, `tunings`, `operations`, ...; read
  from `client.py` when building), `client_key = ClientKey(envs=("GOOGLE_API_KEY",
  "GEMINI_API_KEY"), keyword="api_key", classes={"Client"}, resources="")` (E3).
- `keyword_renames`: `model_name` -> `model`, `content` -> `contents`, `generation_config` ->
  `config` (call assertions on the migrated call, `unrelated.py`).
- `mock_responses`: `models.generate_content`, `models.embed_content`, `models.count_tokens`,
  `chats.create` (object-shaped examples as in 3.3).
- `failure_hints` (marker -> hint):
  `No API key was provided` (patch the client class where it is looked up or set a placeholder
  key, as for openai); `does not have the attribute 'GenerativeModel'` / `'configure'` (a
  patch target that is gone: patch `...genai.Client`); `unexpected keyword argument 'stream'`
  (use `generate_content_stream`); `'coroutine' object is not an async iterable` (add `await`);
  `No module named 'google.generativeai'` (a leftover import or stub);
  `No module named 'google.api_core'` (move to `google.genai.errors`).
- `mock_failure_hint`, `uncalled_mock_hint`, `uncalled_patched_hint`: openai's texts with the
  Gemini names (`Client.return_value.models.generate_content`).
- `pr_why`: "google-generativeai is no longer supported (support ended 2025-11-30, its repository
  is archived); Google's replacement is google-genai, with a different API (`genai.Client`,
  `client.models.generate_content`)." `pr_change` bullets per category, as openai's
  `rules.pr_change`.

### 3.5 Migration-specific checks (`rewrite_checker`)

Each one returns errors in order, as openai's `config` and `streaming` checks do, and has unit
tests with passing and failing rewrites:

1. **`key`** (config_checks.py): a value passed to `configure(api_key=...)` must reach the
   `Client(api_key=...)` the rewritten calls use (same expression, or the same variable /
   attribute). An explicit `GEMINI_API_KEY` read must not become a bare `Client()` (the
   priority change above). A module-level `genai.Client()` with no `api_key=` in a module that
   tests import is an error unless the tests set one of the two variables (reusing
   `code_checks`' key detection, E3). Never accept a literal key in the rewrite.
2. **`settings`** (config_checks.py): every keyword of the original `GenerativeModel(...)` and
   every field of its `GenerationConfig` (`system_instruction`, `temperature`,
   `max_output_tokens`, `response_mime_type`, `safety_settings`, `tools`, ...) appears in the
   `config` of the rewritten calls on that model (as a `GenerateContentConfig` keyword or a
   dict key). A dropped `system_instruction` changes behaviour silently, and no test catches it.
3. **`safety`**: the rewritten safety settings hold the same (category, threshold) pairs as the
   original, after mapping the old shorthands and enums to the new names.
4. **`streaming`** (streaming.py): no `stream=` keyword left on `generate_content` /
   `send_message`; an `async for` over `client.aio...generate_content_stream(...)` or
   `AsyncChat.send_message_stream(...)` must `await` the call; streamed `chunk.text`
   concatenated without a None guard (`+= chunk.text`) is an error, since a chunk can carry no
   text (a function call or the final usage chunk).
5. **`patches`** (test_checks.py): in a rewritten test, every patch target that resolves into
   `google.genai` (directly or through the module under test, `pkg.mod.genai.X`) must exist
   in 2.34.0's public API (`Client`, `types.*`, `errors.*`, `models.Models.*`, ...: a list
   generated from the downloaded wheel and checked into `rules.py`). `patch("app.genai.configure")`
   or `patch("app.genai.GenerativeModel")` left in a test is an error that names
   `app.genai.Client`. `autospec=True` on `Client` is an error (properties), as for openai.

Generic checks that already exist apply unchanged once E1-E3 are in: the tampering rules,
unrelated edits, `code_checks.autospec_client_problems`,
`unkeyed_method_patch_problems` (`patch("google.genai.models.Models.generate_content")` leaves
the constructor running, so it needs a key), and import-time binding of patch targets.

## 4. Out of scope at first

The scanner still reports these (so a scan lists them); the guide says to leave them alone,
and the report lists them as not migrated:

- **Vertex AI mode**: `genai.Client(vertexai=True, project=, location=)`,
  `GOOGLE_GENAI_USE_VERTEXAI`, and the separate old Vertex SDK (`vertexai.generative_models`,
  `google.cloud.aiplatform`). Only the Gemini Developer API (API key) is migrated.
- **Files API**: `genai.upload_file` / `get_file` / `list_files` / `delete_file` ->
  `client.files.upload(file=...)` (state polling and file objects in `contents` differ).
- **Context caching** (`genai.caching.CachedContent`, `GenerativeModel.from_cached_content`),
  **tuning** (`create_tuned_model`, ...), **operations**.
- **Function calling with Python callables** (`tools=[fn]`, `enable_automatic_function_calling`):
  automatic function calling is off by default in the old SDK and on in the new one, so this is
  a behaviour change (question 2).
- **The PaLM-era API** (`genai.generate_text`, `genai.chat`, `genai.generate_embeddings`, from
  0.1-0.3; its models are shut down) and **semantic retrieval / AQA** (`genai.retriever`,
  `answer`): no new-SDK equivalent; reported as removed.
- **Live API, image/video generation (Imagen, Veo), tokens, batches**: not in the old SDK's
  common use (one candidate below calls a Veo model through `generate_content`; skipped).
- **Notebook magics** (`google.generativeai.notebook`, `%%llm`), `.ipynb` files (the scanner
  reads `.py` only, as for openai).
- **LangChain wrappers** (`langchain_google_genai`), a separate library with its own versions.

## 5. Fixtures and tests

Fixtures in `tests/fixtures/google_genai/`, realistic code written for 0.8.x (no secrets, no
real keys):

- `basic_generate.py`: module-level `configure(api_key=os.getenv("GEMINI_API_KEY"))`, a
  `GenerativeModel` with `system_instruction` + `GenerationConfig`, `.text`.
- `provider_class.py`: a provider class with `self.model`, `safety_settings` as a list of
  dicts with lenient thresholds, `try: response.text except ValueError`, usage tokens,
  `google.api_core.exceptions.ResourceExhausted` / `DeadlineExceeded` retries (modelled on
  agentic-tictactoe below).
- `chat_stream.py`: `start_chat(history=...)`, `send_message(stream=True)`, iteration, `resolve()`,
  `chat.history`.
- `embeddings.py`: single and batch `embed_content`, `["embedding"]` access, `task_type`.
- `async_calls.py`: `generate_content_async`, async streaming, `send_message_async`.
- `aliases.py`: `from google import generativeai as gg`, `from google.generativeai import
  GenerativeModel`, `import google.generativeai` (bare), a repo module named `genai`.
- `new_sdk.py`: code already on google-genai (no findings; `new_api_uses` hits).
- `out_of_scope.py`: `upload_file`, `caching`, `tools=[fn]`, `generate_text`.
- `tests_mock.py`: tests that patch `app.genai.GenerativeModel` / `app.genai.configure`,
  `assert_called_once_with(model_name, system_instruction=None)`, `monkeypatch.setenv`, a
  `sys.modules["google.generativeai"]` stub.
- Dependency files: requirements (`google-generativeai==0.8.4`, `>=0.3.0`, unpinned, with
  `google-genai` already listed), pyproject PEP 621 + Poetry, setup.py, Pipfile, environment.yml.

Tests (new files, one per module, as openai-v1's): `test_google_genai_scanner.py` (every row of
3.1, confidence included, no findings on `new_sdk.py`, nothing for names from other `google.*`
packages), `test_google_genai_dependencies.py` (finding + pin edit per file format, the rename
and dedupe, E2), `test_google_genai_guide.py` (part selection), `test_google_genai_checks.py`
(the five checks), `test_google_genai_already_migrated.py`, and engine tests for E1-E5 that
also show openai-v1 is unchanged. Docker tests: a fixture repo whose tests patch the old SDK,
fixed by a scripted diff through `apifix verify` (no LLM): baseline fails without
google-generativeai installed, the fix verifies, with the network off and placeholder keys
only; and the smoke check against the real wheel. One live `-m llm` run on the fixture repo
once the guide is written.

## 6. Benchmark repos

**Shortlist check (`short-google-genai.tsv`, default branches as of 2026-10-30).** None of the
12 still qualifies:

| repo | default branch today |
|---|---|
| federicodeponte/opendraft, langchain-ai/langchain-google, hwfengcs/SDYJ_Multi_Agents, mscraftsman/generative-ai, vlm-run/mm | already on google-genai (old SDK only left in a requirements line or a docstring) |
| gkamradt/SnakeBench | no Gemini code left (both SDKs only in requirements) |
| bolone-sengkuni/hermes-brutal-mod, saulpw/aipl | no old-SDK code (`google.api_core` only, for another Google API) |
| rithwik-01/guardrailed | only `logging.getLogger("google.generativeai")` |
| Mangaba-ai/mangaba_ai | only setup/validation scripts (`check_setup.py`, `scripts/validate_env.py`), no tests reach them |
| TruCoded/codeyy | one `ping_gemini.py` script, no tests |
| rahulbansal16/DictationDaddy | not checked (the clone is very large); can redo if wanted |

**Replacements**, from `cand-google-genai.txt` (140 repos) via GitHub code search for
`"google.generativeai"`, then the git tree for test files, then a shallow clone:

1. **arun-gupta/agentic-tictactoe** (`main`, 13 stars, Python >=3.11, CI). `src/llm/gemini_provider.py`:
   `configure`, `GenerativeModel(..., safety_settings=[...])`, `try: response.text`,
   `.candidates`, `usage_metadata`, retries on `google_exceptions.ResourceExhausted` /
   `DeadlineExceeded` / `ServiceUnavailable` / `Unauthenticated`. `tests/unit/llm/test_gemini_provider.py`
   patches `src.llm.gemini_provider.genai.configure` and `.GenerativeModel` and imports
   `google.api_core.exceptions`. Covers patch targets, errors, safety and `.text`. The repo has
   a TODO to migrate this itself.
2. **thornewolf/ailabel** (`main`, Python >=3.13, CI). `ailabel/lib/llms.py`: `configure(api_key,
   transport="rest")` **at import**, `genai.GenerationConfig(response_mime_type=...)`,
   `GenerativeModel(..., system_instruction=)`; `tests/conftest.py` sets `GEMINI_API_KEY`
   (autouse); `tests/lib/test_llms.py` patches `ailabel.lib.llms.genai.GenerativeModel` and
   asserts `assert_called_once_with(Models.GEMINI_2_0_FLASH, system_instruction=None)`, a call
   assertion the migration must change without weakening it. Covers import-time clients, the
   `settings` check and keyword renames.
3. **Kaizen-agent/kaizen-agent** (`main`, 46 stars, `python_requires>=3.8`). Old SDK in 3 code
   files (`augment.py`, `memory.py`, `autofix/test/test_case.py`: `configure`,
   `GenerativeModel`, `genai.types.GenerationConfig`, `.candidates`, `.parts`), pinned
   `google-generativeai>=0.3.2` in setup.py + requirements. No test reaches the Gemini code,
   so it exercises the unverified / no-signal path, a multi-file fix and the setup.py rename.
   `autofix/test/` is source code, not tests, which checks test detection.
4. Optional: **nrouizem/code-assistant** (`main`): a lazy `import google.generativeai` inside a
   function, `GenerativeModel(model_name=, tools=)`, `.candidates[0].content.parts`, no tests
   reach it. Useful only once function calling is in scope.

Not used: Jerryblessed/shine and ADITYA2024webdev/gods-eye-ai-enhanced (both commit a literal
API key), BladimirFreelance/google.generativeai (a Veo video model through `generate_content`,
which is out of scope; its `sys.modules` stub test is copied into a fixture instead),
trylonai/gateway (logger name only).

Runs go in `benchmark/live/<date>-<repo>-<model>` as the openai ones do, and the shortlist
file gets the three repos above (question 5).

## 7. Order of work (one commit each, full suite + ruff before each)

1. E1-E6 engine changes, openai-v1 unchanged (its tests pass as they are).
2. Package skeleton, rules, scanner and dependency findings, fixtures and scanner tests.
3. Pin edits with the rename, already-migrated, smoke check, sandbox Docker test.
4. Guide parts, migration fields, failure hints, PR text.
5. The five rewrite checks.
6. Live LLM test on the fixture repo; benchmark runs on the three repos, each with its own
   `genai-run`-style plan if fixes are needed.

## 8. Open questions

1. **Pin**: `google-genai>=1.19.0,<3` (pip picks the newest release for the sandbox's Python),
   or a higher floor such as `>=1.47.0` (the newest release that still supports 3.9) to get
   fewer old 1.x releases in fixes?
2. **Function calling**: out of scope at first (reported, not fixed), or in scope with
   `automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)` added
   wherever the old code did not enable it, to keep behaviour?
3. **`google.api_core.exceptions`**: always move them to `google.genai.errors` (my
   recommendation: the new SDK never raises them, so keeping the `except` clauses keeps code
   that can never run), or keep them and add `google-api-core` as a dependency when the repo
   uses them for other Google clients too?
4. **`google-ai-generativelanguage`**: remove a direct declaration when nothing else imports
   `google.ai.generativelanguage`, or only flag it (my default: flag only)?
5. **Benchmarks**: are the three replacements fine (none of the 12 shortlisted repos still uses
   the old SDK in tested code), and should I also check DictationDaddy and rewrite
   `short-google-genai.tsv`?
6. **Files with out-of-scope findings**: fix the in-scope parts and list the rest in the report
   (my default), or skip such a file entirely?

## Approval amendments

1. Pin google-genai>=1.19.0,<3.
2. Function calling out of scope, but preserve behaviour: where old code
   passes Python callables as tools without enabling automatic function
   calling, the fix must disable it explicitly; report these for review.
3. Move google.api_core exceptions to google.genai.errors, verified
   against the source; [likely] where catching becomes broader or
   narrower (e.g. ResourceExhausted -> ClientError code 429). Keep
   google.api_core only when used for something else (e.g. retries).
4. google-ai-generativelanguage: flag only.
5. Use the three proposed repos; skip DictationDaddy.
6. Fix in-scope parts and list the rest, but cap the verdict at
   verified_weak when out-of-scope findings remain, and list them first
   in the PR body.
7. Key order: when old code relied on the environment, keep the old
   precedence explicitly (GEMINI_API_KEY, then GOOGLE_API_KEY); when it
   passed a key to configure(), pass the same key to Client.
8. Never create a Client at import time when the old code didn't need a
   key there (the sandbox has no secrets); create it where used or lazily.
9. After each engine commit, replay a published openai result with
   `apifix verify` (no LLM) to show openai-v1 is unchanged.

Scope of this session: build ONLY the engine changes (dotted-prefix module
matching, pin edits that rename a distribution, multiple key env vars,
verifier key markers from the migration), one commit each, push to origin
main, then stop.

## Built

- **E1 dotted `api_modules`** (`api_names.py`): `in_modules` matches a library module and
  everything under it by dotted prefix (`google.genai` covers `google.genai.types`, not
  `google.cloud`); `module_aliases` maps the local names that imports bind to a library module
  (`import google.generativeai as genai`, `from google import genai`, `import openai as oa`;
  names imported from inside a module, such as `from openai import OpenAI`, are left out). Used
  by `tampering.py` (renamed references, resolved through either version's aliases),
  `suspicious.py` (free imports; its own `_in`/`_resolve` now come from there),
  `unrelated.py` (library-bound names: a plain `import google.genai` does not bind all of
  `google`, its dotted names match instead; patch strings), `code_checks.py` (module index,
  patch targets, the binding check, client classes, keyless clients), `context.py` (library
  imports) and `imports.py` (the migrated library's first name, `google`, is importable).
  openai-v1: a top-level module matches as before; `import openai as oa` now also counts as
  the library in tampering. Replay of `benchmark/live/2026-10-30-aipl-gptoss` with
  `apifix verify`: verification.json (without timings and object addresses) and report.md
  (without timings) identical to the run before the change.
- **E2 distribution rename in pin edits** (`Migration.source_package`, `""` for openai-v1):
  a flagged `google-generativeai` entry becomes `google-genai<pin_spec>` in every format
  pin_edits handles; only the name and spec characters change (extras, markers, comments and
  quoting stay). Where the same file already declares google-genai, the old entry's line is
  removed instead, when nothing else is on it (quotes, a list comma, a YAML dash and a comment
  aside), and a google-genai declaration that allows releases below the pin's `>=` floor is
  raised to the pin. Falls back to the model: an old entry sharing its line with others
  (`deps = ["a", "google-generativeai"]`) when it must be removed, and a name apifix cannot
  locate (a Poetry sub-table `[tool.poetry.dependencies.google-generativeai]`, a dotted key).
  The re-parse checks compare the expected multiset of (name, clauses), the expected TOML
  document, setup.py AST (removed list items dropped) and the conda pip count. Report and PR
  lines say `removed (google-genai is declared in the same file)`. `already_migrated.py`: a
  repo that still declares the old distribution is not migrated. Replay of the aipl result:
  identical.
- **E3 `ClientKey` for several variables** (`envs`, openai-v1: `("OPENAI_API_KEY",)`): any of
  them set counts in `code_checks._sets_key` (setenv, `os.environ[...]`, `patch.dict`), in
  pytest-env settings, and in the repo's CI (`_client_key`: a placeholder for any of them
  turns the unkeyed-method-patch check off); a rewrite may set any of them (`_env_names`).
  The message says `raises when neither GOOGLE_API_KEY nor GEMINI_API_KEY is set` and suggests
  the first variable (openai's text is unchanged). `resource_method` with `resources=""`:
  `google.genai.models.Models.generate_content` -> `models.generate_content` (`AsyncModels`,
  `chats.Chats.create`), only for a module named after its class (not `types.Part`); library
  modules match by dotted name, and a target with nothing after the resources segment no
  longer raises IndexError. Replay of the aipl result: identical.
- **E4 no-signal key markers from the migration** (`Migration.key_failure_markers`;
  openai-v1: `api_key client option must be set`, `OPENAI_API_KEY`, moved out of
  `verifier.EXTERNAL_MARKERS`, which keeps the generic ones including `No API key provided`).
  `compare_runs`, `is_external_failure` and `environment_reasons` take them; `verify` passes the
  migration's. google-genai will add `No API key was provided`, `GOOGLE_API_KEY`,
  `GEMINI_API_KEY`. Replay of the aipl result: identical.
- Not built in this session (by request): E5 minimum Python, E6 registry and docs, and the
  migration package itself.

### Next session

- **E5 minimum Python** (`Migration.min_python`; openai-v1: `"3.8"`, `project.python_at_least`):
  `_Job.run()` detects the repo's Python (`detect_python_version`, the sandbox's
  `default_python` as the default when the fake sandboxes in tests have no `config`) right
  after the `already_migrated` check, before the preflight check or any LLM call, and aborts
  with `python: {target_name} needs Python {min_python} or later; the repo's detected
  interpreter is Python {version} ({source})` when it is older. Replay of
  `benchmark/live/2026-10-30-aipl-gptoss` with `apifix verify`: identical (object addresses and
  timings aside) — `apifix verify` never calls the agent loop, so this change cannot affect it.
- **E6 registry and docs**: `google_genai.MIGRATION` registered in `migrations/__init__._registry`
  (`available_migrations()` now `["google-genai", "openai-v1"]`); CLAUDE.md's layout line and
  README's new "Supported migrations" section updated; docs/notes.md gained "Known limitations
  (google-genai migration)" (see below for what it lists).
- **Migration package** `src/apifix/engine/migrations/google_genai/`: `rules.py` (data tables:
  `PACKAGE`/`SOURCE_PACKAGE`/`PIN_SPEC`/`FIRST_VERSION`/`CLIENT_KEY`/error and module-call
  tables/guide selectors), `python_checks.py` (the scanner: import, `configure()` ->
  `client_config`, `GenerativeModel`/`generate_content`/`generate_content_stream`/
  `count_tokens`/`start_chat`/`send_message`/`embed_content` -> `api_call` with whole-file
  flow-insensitive instance tracking for which names hold a model or a chat, `.text` in
  `except ValueError`/`chat.history`/`embedding["embedding"]`/`.resolve()`/`.to_dict()`/
  `supported_generation_methods` -> `response_access`, removed error classes and
  `google.api_core.exceptions.*` -> `error_class`, `genai.protos` -> `removed_module`),
  `streaming.py` (the `streaming` rewrite check: leftover `stream=`, an un-awaited
  `async for` over `..._stream(...)`, unguarded `chunk.text` concatenation),
  `config_checks.py` (`key`/`settings`/`safety` rewrite checks, presence heuristics rather
  than openai_v1's full dataflow — see docs/notes.md), `test_checks.py` (the `patches` rewrite
  check: a patch target ending in a name google.genai dropped, or `autospec=True` on a
  `Client` target), `repo_findings.py` (`adjust_findings`: names the files with api_call
  findings on a `client_config` finding when they are in another file), `guide_parts.py` +
  `guide.md` (the same part-selector mechanism as openai_v1, no resources-table filtering),
  `__init__.py` (the `Migration` object: `min_python = "3.9"`, `check_dependency` handling both
  `source_package` at any version and a loose/old `package` declaration, failure hints for the
  new SDK's error texts).
  - Fixtures (`tests/fixtures/google_genai/`): `basic_generate.py`, `provider_class.py` (self
    attribute tracking, `google.api_core.exceptions`), `chat_stream.py`, `embeddings.py`,
    `aliases.py` (bare `import google.generativeai`, `from google import generativeai as gg`),
    `new_sdk.py` (no findings), `out_of_scope.py`, `tests_mock.py`.
  - Tests: `test_google_genai_scanner.py` (8), `test_google_genai_dependencies.py` (11,
    including the rename and the already-declared-google-genai raise-to-floor case),
    `test_google_genai_guide.py` (8), `test_google_genai_checks.py` (15, three per check plus
    one extra), `test_google_genai_already_migrated.py` (4). `tests/test_migrations.py`'s
    registry test updated for the second migration. Full suite (no `--docker`/`--llm`): 2052
    passed. `ruff check .` and `ruff format --check .`: clean.
  - Final replay of `benchmark/live/2026-10-30-aipl-gptoss` with `apifix verify`, after E6 and
    the migration package: identical to the E1-E4 replay (object addresses and timings aside).
  - **Not built in this session** (narrower than the plan's section 7, by necessity of scope —
    see docs/notes.md "Known limitations (google-genai migration)" for the specifics): no
    string-reference (`mock.patch("...")`) scanner findings; the `key`/`settings`/`safety`/
    `patches` checks are heuristics, not openai_v1-grade dataflow; no safety-settings scanner
    row (handled only through the generic `GenerativeModel` finding plus the `safety` rewrite
    check); no dedicated PaLM-era/semantic-retrieval scanner rows beyond the `MODULE_CALLS`
    entries already covering `generate_text`/`chat`/`generate_embeddings`; no Docker sandbox
    test of the smoke check or a fixture-repo fix; no live `-m llm` run; no benchmark-repo runs
    (`agentic-tictactoe`, `ailabel`, `kaizen-agent`) — these were explicitly out of scope for
    this session.
