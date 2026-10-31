# google-generativeai -> google-genai

google-generativeai (the SDK behind `import google.generativeai as genai`) is no longer
supported: Google ended support on 2025-11-30 and archived its repository. Its replacement is
google-genai (`from google import genai`), with a different, client-based API.

Only the Gemini Developer API (an API key) is migrated here. Vertex AI mode
(`genai.Client(vertexai=True, ...)`, `GOOGLE_GENAI_USE_VERTEXAI`), the Files API, context
caching, tuning, operations, automatic function calling with Python callables, and the
PaLM-era functions (`genai.generate_text`, `genai.chat`, `genai.generate_embeddings`) are out of
scope: leave them alone, and report them in the summary rather than guessing at a fix.

<!-- part: client -->
## Client

`genai.configure(api_key=X)` becomes `client = genai.Client(api_key=X)`. Build the client where
it is used, not at import time: the old `configure()` could be called with no key yet (the key
was only needed later, at the first call); `genai.Client()` raises immediately if no key is
available, so building one at import time breaks code that used to defer this.

Never drop a key value. If the old code called `configure()` with an explicit key
(`configure(api_key=my_key)`), pass that same expression to `Client(api_key=my_key)`. If the old
code called `configure()` with no key at all, relying on the environment, `genai.Client()` reads
the environment itself too — but the two SDKs disagree on which variable wins when both are set:
the old SDK read `GEMINI_API_KEY` first, falling back to `GOOGLE_API_KEY`; the new SDK's default
prefers `GOOGLE_API_KEY`. If the old code (or its surrounding deployment) could plausibly have
both set, keep the old precedence explicitly:
`genai.Client(api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))`.

<!-- part: client.async -->
## Async

`client.aio.models` and `client.aio.chats` mirror the sync API (`generate_content`,
`embed_content`, `count_tokens`, `chats.create`), each `async def`. The async stream needs an
`await` before the `async for`, since the method itself is a coroutine that returns an async
iterator: `async for chunk in await client.aio.models.generate_content_stream(...)`.

<!-- part: models -->
## Models and calls

There is no model object in the new SDK. `GenerativeModel(name, system_instruction=...,
generation_config=..., safety_settings=..., tools=..., tool_config=...)` becomes: keep the model
name (add a `models/` prefix is not needed, the new SDK does it itself), and move every one of
those keywords (plus every field of a `GenerationConfig`) into a single
`types.GenerateContentConfig(...)` passed on each call. A class that kept `self.model =
GenerativeModel(...)` keeps `self.client` and `self.model_name` plus one shared config built
once.

- `<model>.generate_content(c, ...)` -> `client.models.generate_content(model=name, contents=c,
  config=types.GenerateContentConfig(...))`
- `<model>.count_tokens(c)` -> `client.models.count_tokens(model=name, contents=c)`
- `genai.list_models()` -> `client.models.list()`; `genai.get_model(name)` ->
  `client.models.get(model=name)`

Dropping one of the moved keywords silently changes behaviour (a missing `system_instruction`,
a missing `safety_settings` entry): carry every one of them into the config.

<!-- part: chat -->
## Chat

`<model>.start_chat(history=...)` -> `client.chats.create(model=name, config=..., history=...)`.
`send_message` keeps its name; `chat.history` becomes the method `chat.get_history()` (curated
by default; `curated=False` for the raw turns). There is no `rewind()` or `.last`; keep the last
response yourself if the code used them.

<!-- part: embeddings -->
## Embeddings

`genai.embed_content(model=, content=, task_type=, title=, output_dimensionality=)` becomes
`client.models.embed_content(model=, contents=, config=types.EmbedContentConfig(task_type=...,
title=..., output_dimensionality=...))` — `content` renames to `contents`, and the per-field
keywords move into `config`. The result is an object, not a dict:
`result.embeddings[0].values` (a batch: `[e.values for e in result.embeddings]`), not
`result["embedding"]`.

<!-- part: responses -->
## Responses

`.text` returns `None` instead of raising `ValueError` when there is no text (a blocked prompt,
a function-call-only response): change a `try: ... except ValueError:` around it into a `None`
check. `usage_metadata` keeps the same field names (`prompt_token_count`,
`candidates_token_count`, `total_token_count`). `response.resolve()` is gone (iterate the stream
to completion instead); `response.to_dict()` is `response.model_dump()`;
`Model.supported_generation_methods` is renamed `supported_actions`.

<!-- part: streaming -->
## Streaming

`generate_content(..., stream=True)` -> `client.models.generate_content_stream(...)`;
`chat.send_message(m, stream=True)` -> `chat.send_message_stream(m)`. Chunks have `.text`, which
may be `None` (a function-call-only chunk or the final usage chunk): guard any concatenation
(`text += chunk.text or ""`), never `text += chunk.text` unguarded. There is no `resolve()` on
the new stream.

<!-- part: safety -->
## Safety settings

Old shapes (a dict `{HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE}`, a
list of such dicts, or shorthand strings like `{"harassment": "block_none"}`) all become a list
of `types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE")`. Keep
every category and threshold exactly as it was — the string enum values are the same names,
upper-cased (`"harassment"` -> `HARM_CATEGORY_HARASSMENT`, `"block_none"` -> `BLOCK_NONE`), but
nothing besides the old shorthand `"harassment"`-style strings still works, so always write out
the enum name.

<!-- part: errors -->
## Errors

`genai.types.BlockedPromptException`, `StopCandidateException`, `IncompleteIterationError` and
`BrokenResponseError` are gone: check `response.prompt_feedback.block_reason` or
`candidates[0].finish_reason` instead of catching an exception.

`google.api_core.exceptions.*` (raised by the old SDK's transport) have no equivalent in the new
one, which never raises them and does not depend on google-api-core at all:
`ResourceExhausted` / `InvalidArgument` / `PermissionDenied` / `Unauthenticated` / `NotFound` /
`FailedPrecondition` -> `google.genai.errors.ClientError` (check `e.code` for the HTTP status,
e.g. 429 for a quota error); `DeadlineExceeded` / `ServiceUnavailable` / `InternalServerError`
-> `google.genai.errors.ServerError`; `GoogleAPICallError` / `GoogleAPIError` ->
`google.genai.errors.APIError` (the common base, 4xx and 5xx alike). Keep `google.api_core` only
where the file uses it for something besides the Gemini calls (another Google client, or
retries on the exception types themselves rather than catching them) — do not remove an import
still needed for that.

<!-- part: tests -->
## Tests

Patch `pkg.mod.genai.Client`, not `genai.configure` or `genai.GenerativeModel` (those attributes
no longer exist on the module, so a leftover patch of them fails well into the test run, not at
collection). Set `Client.return_value.models.generate_content.return_value` (or `.chats.create`,
`.models.embed_content`). Never patch the client class with `autospec=True`: its resources
(`models`, `chats`, `aio`, ...) are properties, which autospec does not follow, leaving a mock
without them. A mock response is `MagicMock(text="...")`, or a real
`types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model",
parts=[types.Part(text="...")]))])`. Simulate an error with
`errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status":
"RESOURCE_EXHAUSTED"}})`. A `sys.modules["google.generativeai"]` stub becomes a stub of
`google.genai`; `monkeypatch.setenv("GEMINI_API_KEY", ...)` keeps working (the new SDK reads it
too, as a fallback).

<!-- part: dependencies -->
## Dependencies

Replace `google-generativeai` with `google-genai` wherever it is declared — never keep both.
apifix sets this pin itself from the scanner's dependency findings; it needs no fix from you.
