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
- `request_timeout=` becomes `timeout=`. `engine=` / `deployment_id=` become `model=`.

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

<!-- part: responses.streaming -->
Streaming (`stream=True`) yields objects too:

```python
for chunk in client.chat.completions.create(model=m, messages=msgs, stream=True):
    if chunk.choices and chunk.choices[0].delta.content:
        out.append(chunk.choices[0].delta.content)
```

<!-- part: errors -->
## Errors

`openai.error.X` moved to `openai.X`:

| 0.x | 1.x |
|---|---|
| `openai.error.OpenAIError`, `RateLimitError`, `APIError`, `AuthenticationError`, `APIConnectionError` | same name at `openai.` |
| `openai.error.InvalidRequestError` | `openai.BadRequestError`; 1.x also has `NotFoundError`, `UnprocessableEntityError`, `ConflictError` for other statuses, so an `except` may need several classes |
| `openai.error.Timeout` | `openai.APITimeoutError` |
| `openai.error.ServiceUnavailableError` | `openai.InternalServerError` |
| `openai.error.PermissionError` | `openai.PermissionDeniedError` |
| `openai.error.TryAgain` | removed; the client retries itself (`max_retries=`) |
| `openai.error.SignatureVerificationError` | removed |
| `openai.error.InvalidAPIType` | removed; use `openai.AzureOpenAI` for Azure |

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
test calls, never the function or class the test exercises: after `from app import ask`,
`patch("app.ask")` leaves the test's `ask` untouched, so the real one runs.

Mock responses must be objects, not dicts: 1.x code reads `resp.choices[0].message.content`,
which fails on a dict. Helpers that build dict responses must return objects:

```python
# 0.x
def completion(text):
    return {"choices": [{"message": {"content": text}}]}


# 1.x
from types import SimpleNamespace


def completion(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])
```

`openai.OpenAI()` raises `OpenAIError` when no API key is set (argument or `OPENAI_API_KEY`),
and tests normally run without one. A test must keep the real constructor from running, or the
module must not build a client at import. Pick the pattern from how the migrated module gets
its client:

```python
# 0.x
@mock.patch("openai.ChatCompletion.create")
def test_answer(create):
    create.return_value = {"choices": [{"message": {"content": "hi"}}]}


# 1.x, client created inside the function (def answer(): client = openai.OpenAI() ...)
@mock.patch("app.openai.OpenAI")
def test_answer(OpenAI):
    OpenAI.return_value.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))]
    )


# 1.x, module-level client (client = openai.OpenAI() at the top of app.py; the module must
# import without a key)
@mock.patch("app.client.chat.completions.create")
def test_answer(create):
    create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))]
    )


# 1.x, lazily built client (def get_client() in app.py)
@mock.patch("app.get_client")
def test_answer(get_client):
    get_client.return_value.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))]
    )
```

- When the module does `from openai import OpenAI`, patch `app.OpenAI` instead of
  `app.openai.OpenAI`.
- With any client, patching the class method also works:
  `mock.patch("openai.resources.chat.completions.Completions.create")`.
- Mock return values must support attribute access now that the code uses it; keep the same
  values. `SimpleNamespace` (`from types import SimpleNamespace`) or the real types
  (`openai.types.chat.ChatCompletion`) both work.
- Exception classes in `pytest.raises` / `except` follow the error table; do not replace a
  specific class with a broader one (`Exception`, `openai.APIError`, `openai.OpenAIError`).

<!-- part: dependencies -->
## Dependencies

Pin `openai>=1.0,<2` wherever openai is declared (requirements files, pyproject.toml, setup.py,
setup.cfg, Pipfile). Leave other dependencies alone, except exact pins that hold openai back:
current openai releases need `typing_extensions>=4.11`, and releases before 1.55.3 do not work
with `httpx>=0.28`. In a fully pinned file, raise such pins (e.g. `typing_extensions==4.5.0`)
rather than let pip fall back to an old openai.
