# openai 0.x -> 1.x

<!-- part: client -->
## Client

0.x configured the module; 1.x uses a client object.

```python
# 0.x
openai.api_key = os.getenv("OPENAI_API_KEY")
openai.api_base = "https://example.test/v1"
openai.organization = "org-x"
resp = openai.ChatCompletion.create(model="gpt-4o-mini", messages=msgs)

# 1.x: create the client where it is used, not at import time
from openai import OpenAI


def ask(msgs):
    client = OpenAI(
        base_url="https://example.test/v1", organization="org-x"
    )  # reads OPENAI_API_KEY
    resp = client.chat.completions.create(model="gpt-4o-mini", messages=msgs)
```

- `OpenAI()` reads `OPENAI_API_KEY` from the environment itself: do not add
  `api_key=os.getenv("OPENAI_API_KEY")` or `import os` for it. Pass `api_key=` only where
  0.x code set a key from somewhere else.
- Prefer creating the client where it is used: inside the function that calls the API, or
  lazily through a small cached function (`functools.lru_cache` on `get_client()`). Avoid a
  module-level `client = OpenAI()`: `OpenAI()` raises at construction when no API key is
  available (argument or `OPENAI_API_KEY`), and tests run without keys, so a client built at
  import time breaks every test that imports the module. Never hardcode a real key.
<!-- part: client.async -->
- Async code: `AsyncOpenAI` and `await client.chat.completions.create(...)` (0.x `acreate`).
<!-- part: client.azure -->
- Azure (`api_type = "azure"`): `AzureOpenAI(azure_endpoint=<api_base>, api_version=<api_version>,
  api_key=...)`; `deployment_id=` / `engine=` become `model=<deployment name>`.
<!-- part: client.kwargs -->
- Per-call `api_key=` / `organization=` / `api_base=` move to the client (or
  `client.with_options(...)`).
- `request_timeout=` becomes `timeout=`, `headers=` becomes `extra_headers=`, `request_id=` goes.
  `engine=` / `deployment_id=` become `model=` (on Azure, the deployment name). 1.x methods
  raise TypeError on any of these, also when they reach the method through a wrapper:
  `call_with_retry(func=client.completions.create, engine=e)` needs `model=e`.
<!-- part: client.config -->
- **Never drop a configured value** (`openai.api_key = <value>`, `organization`, `api_base`).
  `OpenAI()` reads only its arguments and the environment (`OPENAI_API_KEY`, `OPENAI_ORG_ID`,
  `OPENAI_BASE_URL`), never `openai.api_key`. Either keep the assignment and call the
  module-level client, which reads `openai.api_key` / `organization` / `base_url` for each
  request:

  ```python
  openai.api_key = key  # unchanged
  resp = openai.chat.completions.create(model=m, messages=msgs)
  ```

  or pass the value to every client: `OpenAI(api_key=key)`. When the assignment is in one module
  (e.g. a CLI that takes `--api-key`) and the API calls are in another, keep the assignment and
  use `openai.chat.completions.create(...)` at the call sites. 1.x ignores `openai.api_base`:
  assign `openai.base_url` instead. An assignment from the variable 1.x reads itself
  (`os.getenv("OPENAI_API_KEY")`) can go; 0.x's `OPENAI_API_BASE` is not one (1.x reads
  `OPENAI_BASE_URL`). Async code: `AsyncOpenAI(api_key=openai.api_key)` where it is used.
- Tests of code that uses the module-level client: patching `openai.OpenAI` does not reach it.
  Patch `openai.chat.completions.create`, with a key set (`OPENAI_API_KEY` or
  `openai.api_key = "test"`): the module client is built on first use, and that needs a key.

<!-- part: resources -->
## Resources

| 0.x | 1.x |
|---|---|
| `openai.ChatCompletion.create` / `acreate` | `client.chat.completions.create` |
| `openai.Completion.create` | `client.completions.create` |
| `openai.Embedding.create(input=..., engine=...)` | `client.embeddings.create(input=..., model=...)` |
| `openai.Image.create` / `create_edit` / `create_variation` | `client.images.generate` / `edit` / `create_variation` |
| `openai.Audio.transcribe(model, file)` / `translate` | `client.audio.transcriptions.create(model=, file=)` / `client.audio.translations.create` |
| `openai.Moderation.create` | `client.moderations.create` |
| `openai.File.create` / `list` / `retrieve` / `delete` / `download` | `client.files.create` / `list` / `retrieve` / `delete` / `content` |
| `openai.FineTuningJob.create` / `list` / `retrieve` / `cancel` / `list_events` | `client.fine_tuning.jobs.*` (same names) |
| `openai.Model.list` / `retrieve` | `client.models.list` / `retrieve` |

<!-- part: resources.removed -->
Removed with no direct equivalent:
- `openai.Edit` (use `client.chat.completions.create` with an instruction prompt),
  `openai.Engine` (use `client.models`), `openai.Customer`, `openai.Deployment` (manage Azure
  deployments outside the SDK), and legacy `openai.FineTune` (migrate manually to
  `client.fine_tuning.jobs`; parameters differ).
- Modules: `openai.embeddings_utils` (copy the helpers you need, e.g. `cosine_similarity` with
  numpy), `openai.openai_object` / `openai.openai_response` (responses are pydantic models),
  `openai.api_resources` / `openai.api_requestor` (use the client), `openai.util`,
  `openai.cli`, `openai.wandb_logger`, `openai.datalib`, `openai.validators`.

<!-- part: responses -->
## Responses are objects, not dicts

```python
# 0.x
text = resp["choices"][0]["message"]["content"]
tokens = resp.get("usage", {}).get("total_tokens")
vector = resp["data"][0]["embedding"]

# 1.x
text = resp.choices[0].message.content
tokens = resp.usage.total_tokens if resp.usage else None
vector = resp.data[0].embedding
```

Use `resp.model_dump()` where code really needs a dict (JSON output, storage). Completions:
`resp.choices[0].text`. Images: `resp.data[0].url`. Transcriptions: `resp.text`.
Moderations: `resp.results[0].flagged`.

<!-- part: streaming -->
## Streaming

`stream=True` yields chunk objects too. In 1.x `chunk.choices[0].delta.content` always exists
and is `None` on the role chunk (the first) and the final chunk, so 0.x's key checks
(`"content" in delta`, `hasattr(delta, "content")`) must become `if delta.content is not None:`
(or `delta.content or ""` where text is joined). `hasattr` is always true in 1.x, so it would
yield the `None`. `chunk.choices` can be empty (Azure's first chunk; the last one with
`stream_options={"include_usage": True}`).

```python
# 0.x
for chunk in openai.ChatCompletion.create(model=m, messages=msgs, stream=True):
    delta = chunk["choices"][0]["delta"]
    if "content" in delta:
        yield delta["content"]

# 1.x
for chunk in client.chat.completions.create(model=m, messages=msgs, stream=True):
    if not chunk.choices:
        continue
    delta = chunk.choices[0].delta
    if delta.content is not None:
        yield delta.content
```

Mocked chunks in tests become objects with the same keys, role included:
`{"choices": [{"delta": {"role": "assistant", "content": "Hi"}}]}` becomes
`SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(role="assistant", content="Hi"))])`.

<!-- part: errors -->
## Errors

`openai.error.X` moved to `openai.X`:

| 0.x | 1.x |
|---|---|
| `openai.error.OpenAIError`, `RateLimitError`, `AuthenticationError`, `APIConnectionError` | same name at `openai.` |
| `openai.error.APIError` | `openai.APIError`, but it now means something else (below) |
| `openai.error.InvalidRequestError` | `openai.BadRequestError`; 1.x also has `NotFoundError`, `UnprocessableEntityError`, `ConflictError` for other statuses, so an `except` may need several classes |
| `openai.error.Timeout` | `openai.APITimeoutError` |
| `openai.error.ServiceUnavailableError` | `openai.InternalServerError` |
| `openai.error.PermissionError` | `openai.PermissionDeniedError` |
| `openai.error.TryAgain` | removed; the client retries itself (`max_retries=`) |
| `openai.error.SignatureVerificationError` | removed |
| `openai.error.InvalidAPIType` | removed; use `openai.AzureOpenAI` for Azure |

`APIError` changed meaning. In 0.x it was a sibling of the other classes, raised for 5xx
responses other than 503 (`ServiceUnavailableError`), statuses without a class of their own,
malformed responses and error events in a stream; never for 400/401/403/404/409/429. In 1.x it
is the base class of every API error: `BadRequestError`, `AuthenticationError`,
`RateLimitError`, `APIConnectionError` and the rest all subclass it. So a retry list or
`except` that names 0.x `APIError` next to `RateLimitError` would, kept as `openai.APIError`,
also retry or swallow 400 and 401. For server errors use `openai.InternalServerError` (every
5xx), or drop `APIError` when `InternalServerError` (0.x `ServiceUnavailableError`) is already
in the tuple. Keep `openai.APIError` only where a catch-all is meant; 1.x raises it directly
for an error event in a stream.

```python
# 0.x
call_with_retry(exceptions_to_retry=(APIError, Timeout, RateLimitError, ServiceUnavailableError))
# 1.x
call_with_retry(exceptions_to_retry=(APITimeoutError, RateLimitError, InternalServerError))
```

1.x status errors need an HTTP response to construct, e.g. in tests:

```python
import httpx

request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
openai.RateLimitError("slow down", response=httpx.Response(429, request=request), body=None)
openai.APIConnectionError(request=request)
```

<!-- part: tests -->
## Tests

Update mocks to the new call path and keep every assertion as it is. Patch what the code under
test calls, never the function or class the test exercises: after `from yourpkg.module
import ask`, `patch("yourpkg.module.ask")` leaves the test's `ask` untouched, so the real one
runs. `yourpkg.module` in these examples is a placeholder: write the dotted path the tests
import the code under test by (`patch("yourpkg.module.OpenAI")` fails with
`ModuleNotFoundError`).

Mock responses must be objects, not dicts: 1.x code reads `resp.choices[0].message.content`,
which fails on a dict. Helpers that build dict responses must return objects:

```python
# 0.x
def completion(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


# 1.x
from types import SimpleNamespace


def completion(text):
    message = SimpleNamespace(role="assistant", content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])
```

`openai.OpenAI()` raises `OpenAIError` when no API key is set (argument or `OPENAI_API_KEY`),
and tests normally run without one; code that catches the error then never calls the mock
("Called 0 times"). A test must keep the real constructor from running, or the module must not
build a client at import. Patch the client class (without autospec), picking the pattern from
how the migrated module gets its client:

```python
# 0.x
@mock.patch("openai.ChatCompletion.create")
def test_answer(create):
    create.return_value = {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}


# 1.x, client created inside the function (def answer(): client = openai.OpenAI() ...)
# (no autospec=True on a client class: an autospecced client has no chat.completions)
@mock.patch("yourpkg.module.openai.OpenAI")
def test_answer(OpenAI):
    OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content="hi"))]
    )


# 1.x, module-level client (client = openai.OpenAI() at the top of yourpkg/module.py; the
# module must import without a key)
@mock.patch("yourpkg.module.client.chat.completions.create")
def test_answer(create):
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content="hi"))]
    )


# 1.x, lazily built client (def get_client() in yourpkg/module.py)
@mock.patch("yourpkg.module.get_client")
def test_answer(get_client):
    get_client.return_value.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content="hi"))]
    )
```

Patching the method instead leaves the real constructor running, so unless the code passes
`api_key=`, the test must also set a placeholder key:

```python
# 1.x, the method patched (any client); the constructor still runs and needs a key
@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"})
@mock.patch("openai.resources.chat.completions.Completions.create")
def test_answer(create):
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content="hi"))]
    )


# (pytest: monkeypatch.setenv("OPENAI_API_KEY", "test") in the test or a fixture)
```

- When the module does `from openai import OpenAI`, patch `yourpkg.module.OpenAI` instead of
  `yourpkg.module.openai.OpenAI`.
- Drop `autospec=True` when patching a client class (`patch("yourpkg.module.OpenAI",
  autospec=True)`): an autospecced 1.x client has no `chat.completions` (resources are cached
  properties), so `OpenAI.return_value.chat.completions.create` fails. Patch the class
  without autospec. If you patch the method instead (`...Completions.create`), the real
  client is still built, so set `OPENAI_API_KEY` in the test as above; with `autospec=True`
  the method's mock also receives the resource object as its first argument
  (`call_args.args[0]`), so assertions such as `args == ()` fail.
- Mock return values must support attribute access now that the code uses it; keep the same
  values. `SimpleNamespace` (`from types import SimpleNamespace`) or the real types
  (`openai.types.chat.ChatCompletion`) both work.
- Keep each test's assertions: migrate the ones it has and add none. A new
  `create.assert_called_once()` in a test that never reached `create` before (its code raises
  or returns first) fails.
- Exception classes in `pytest.raises` / `except` follow the error table; do not replace a
  specific class with a broader one (`Exception`, `openai.APIError`, `openai.OpenAIError`).

<!-- part: dependencies -->
## Dependencies

Pin `openai>=1.55.3,<3` wherever openai is declared (requirements files, pyproject.toml, setup.py,
setup.cfg, Pipfile); 2.x keeps the 1.x API used here. Leave other dependencies alone, except exact
pins that hold openai back: openai 1.55.3 and later need `typing_extensions>=4.11` (recent 2.x
releases `>=4.14`), and releases before 1.55.3 do not work with `httpx>=0.28`. In a fully pinned
file, raise such pins (e.g. `typing_extensions==4.5.0`) rather than let pip fall back to an old
openai.
