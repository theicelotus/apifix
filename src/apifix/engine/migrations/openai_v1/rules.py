"""Rule tables for the openai 0.x -> 1.x migration. Pure data, no logic."""

from __future__ import annotations

from apifix.engine.models import CERTAIN, LIKELY, Confidence

CLIENT = "client = openai.OpenAI()"
ASYNC_CLIENT = "client = openai.AsyncOpenAI()"
AZURE_CLIENT = "openai.AzureOpenAI(azure_endpoint=..., api_version=..., api_key=...)"
AZURE_CLIENT_ASSIGN = f"client = {AZURE_CLIENT}"
ASYNC_AZURE_CLIENT_ASSIGN = "client = openai.AsyncAzureOpenAI(azure_endpoint=..., api_version=...)"

# Class-based 0.x resources -> attribute path on the 1.x client.
RESOURCES: dict[str, str] = {
    "ChatCompletion": "chat.completions",
    "Completion": "completions",
    "Embedding": "embeddings",
    "Image": "images",
    "Moderation": "moderations",
    "Audio": "audio",
    "File": "files",
    "FineTuningJob": "fine_tuning.jobs",
    "Model": "models",
}

# Sync 0.x method names; "a" + name is the async variant (acreate, alist, atranscribe, ...).
SYNC_METHODS = {
    "create",
    "list",
    "retrieve",
    "delete",
    "cancel",
    "list_events",
    "transcribe",
    "translate",
    "create_edit",
    "create_variation",
    "download",
}

# (resource, 0.x method) -> 1.x method path when it differs from the 0.x name.
METHOD_RENAMES: dict[tuple[str, str], str] = {
    ("Image", "create"): "generate",
    ("Image", "create_edit"): "edit",
    ("Audio", "transcribe"): "transcriptions.create",
    ("Audio", "translate"): "translations.create",
    ("File", "download"): "content",
}

REMOVED_RESOURCES: dict[str, str] = {
    "FineTune": (
        "Legacy fine-tunes API removed; migrate manually to client.fine_tuning.jobs "
        "(different params)"
    ),
    "Edit": "Edits API removed; use client.chat.completions.create with an instruction prompt",
    "Engine": "Engines API removed; use client.models",
    "Customer": "Removed in 1.x; no equivalent",
    "Deployment": "Removed in 1.x; manage Azure deployments outside the SDK",
}

# Per-call kwargs that 1.x renamed or moved onto the client.
KWARG_CHANGES: dict[str, str] = {
    "engine": "Renamed: pass model=... instead of engine=...",
    "deployment_id": "Azure: pass the deployment name as model=... on openai.AzureOpenAI",
    "request_timeout": "Renamed: timeout=... (or client.with_options(timeout=...))",
    "headers": "Renamed: extra_headers=...",
    "api_key": "Per-call credentials removed: set api_key on the client (or client.with_options)",
    "organization": "Per-call organization removed: set organization on the client",
    "api_base": "Per-call api_base removed: set base_url on the client",
    "api_type": "Per-call api_type removed: use openai.AzureOpenAI for Azure",
    "api_version": "Per-call api_version removed: set api_version on openai.AzureOpenAI",
}

# Module-level config attribute -> (suggestion, suggestion when the file targets Azure, confidence).
# api_key / organization / api_type / api_version still exist on 1.x's module-level client, so
# they are "likely" rather than "certain" to break.
CONFIG_ATTRS: dict[str, tuple[str, str, Confidence]] = {
    "api_key": (
        "Pass it to the client: openai.OpenAI(api_key=...)",
        f"Pass it to the client: {AZURE_CLIENT}",
        LIKELY,
    ),
    "organization": (
        "Pass it to the client: openai.OpenAI(organization=...)",
        "Not used by Azure; drop it or pass organization= to openai.AzureOpenAI",
        LIKELY,
    ),
    "api_base": (
        "Renamed: openai.OpenAI(base_url=...)",
        f"api_base becomes azure_endpoint: {AZURE_CLIENT}",
        CERTAIN,
    ),
    "api_type": (
        "Removed: pick the client class instead (openai.OpenAI or openai.AzureOpenAI)",
        f"Replace api_type='azure' with the Azure client: {AZURE_CLIENT}",
        LIKELY,
    ),
    "api_version": (
        "Only used by Azure: openai.AzureOpenAI(api_version=...)",
        f"Pass it to the client: {AZURE_CLIENT}",
        LIKELY,
    ),
    "api_key_path": (
        "Removed: read the key yourself and pass openai.OpenAI(api_key=...)",
        f"Removed: read the key yourself and pass it to {AZURE_CLIENT}",
        CERTAIN,
    ),
    "proxy": (
        "Removed: pass http_client=httpx.Client(proxy=...) to openai.OpenAI()",
        "Removed: pass http_client=httpx.Client(proxy=...) to openai.AzureOpenAI()",
        CERTAIN,
    ),
    "requestssession": (
        "Removed: pass http_client=httpx.Client(...) to openai.OpenAI()",
        "Removed: pass http_client=httpx.Client(...) to openai.AzureOpenAI()",
        CERTAIN,
    ),
    "aiosession": (
        "Removed: pass http_client=httpx.AsyncClient(...) to openai.AsyncOpenAI()",
        "Removed: pass http_client=httpx.AsyncClient(...) to openai.AsyncAzureOpenAI()",
        CERTAIN,
    ),
}

INVALID_REQUEST_NOTE = (
    "1.x splits this by HTTP status (BadRequestError, NotFoundError, UnprocessableEntityError, "
    "ConflictError); except blocks may need several classes, or openai.APIStatusError"
)

# openai.error.<old> -> (1.x name at openai.<new>, or None if removed; note; confidence)
ERROR_CLASSES: dict[str, tuple[str | None, str, Confidence]] = {
    "OpenAIError": ("OpenAIError", "", CERTAIN),
    "APIError": ("APIError", "", CERTAIN),
    "APIConnectionError": ("APIConnectionError", "", CERTAIN),
    "AuthenticationError": ("AuthenticationError", "", CERTAIN),
    "RateLimitError": ("RateLimitError", "", CERTAIN),
    "InvalidRequestError": ("BadRequestError", INVALID_REQUEST_NOTE, LIKELY),
    "Timeout": ("APITimeoutError", "", CERTAIN),
    "ServiceUnavailableError": ("InternalServerError", "", CERTAIN),
    "PermissionError": ("PermissionDeniedError", "", CERTAIN),
    "TryAgain": (None, "Removed; the 1.x client retries automatically (max_retries=...)", CERTAIN),
    "SignatureVerificationError": (None, "Removed; no equivalent in 1.x", CERTAIN),
    "InvalidAPIType": (None, "Removed; use openai.AzureOpenAI for Azure", CERTAIN),
}

# 0.x re-exported these at openai.* as well as openai.error.*; 1.x no longer has them.
TOP_LEVEL_ERROR_REEXPORTS = {"InvalidRequestError"}

REMOVED_MODULES: dict[str, str] = {
    "embeddings_utils": (
        "Removed in 1.x; copy the helpers you need (e.g. cosine_similarity with numpy) "
        "into your code"
    ),
    "util": "Internal helpers removed in 1.x",
    "cli": "Removed as a module; use the `openai` command-line tool",
    "wandb_logger": "Removed in 1.x; use the W&B OpenAI integration directly",
    "openai_object": "OpenAIObject removed; responses are pydantic models (use .model_dump())",
    "openai_response": "Removed in 1.x; responses are pydantic models",
    "api_resources": "Removed in 1.x; use resources on the client (client.chat.completions...)",
    "api_requestor": "Removed in 1.x; use the client",
    "datalib": "Removed in 1.x",
    "validators": "Removed in 1.x",
}

RESPONSE_ACCESS = (
    "1.x returns typed objects, not dicts: use attribute access "
    "(e.g. resp.choices[0].message.content) or resp.model_dump() for a dict"
)
STREAM_ACCESS = (
    "1.x stream chunks are typed objects: use chunk.choices[0].delta.content "
    "(delta.content may be None)"
)
DICT_METHODS = {"get", "keys", "values", "items", "to_dict", "to_dict_recursive"}
JSON_DUMP = "Responses are not JSON-serialisable as-is: use resp.model_dump_json() / model_dump()"

PACKAGE = "openai"
FIRST_V1 = "1.0"
DEPENDENCY = "Pin openai>=1.0 (e.g. openai>=1.0,<2) and migrate call sites"

# Guide parts (guide.md `<!-- part: id -->` markers) and when a job's prompt includes them.
# A selector (categories, keywords) matches a finding whose category is in `categories` (empty:
# any) and whose old_pattern or suggestion contains one of `keywords` (empty: no condition).
GUIDE_ALWAYS = frozenset({"client"})
GUIDE_FOR_TEST_FILES = frozenset({"tests"})  # when a finding is in a test file
GUIDE_SELECTORS: dict[str, tuple[tuple[frozenset[str], tuple[str, ...]], ...]] = {
    "client.async": ((frozenset(), ("AsyncOpenAI", "await ", "aiosession")),),
    "client.azure": ((frozenset(), ("Azure", "azure", "deployment_id")),),
    "client.kwargs": (
        (frozenset({"client_config"}), ()),
        (frozenset({"api_call"}), tuple(f"{kwarg}=" for kwarg in KWARG_CHANGES)),
    ),
    "resources": ((frozenset({"api_call", "import"}), ()),),
    "resources.removed": (
        (frozenset({"removed_module"}), ()),
        (frozenset({"api_call", "import"}), tuple(f"{name}." for name in REMOVED_RESOURCES)),
    ),
    "responses": ((frozenset({"api_call", "response_access"}), ()),),
    "responses.streaming": ((frozenset({"api_call", "response_access"}), ("stream", "delta")),),
    "errors": (
        (frozenset({"error_class"}), ()),
        (frozenset({"import"}), tuple(ERROR_CLASSES)),
    ),
    "dependencies": ((frozenset({"dependency"}), ()),),
}

# Names a rewrite may use without importing them, and the one import that defines each
# (apifix adds it; see code_checks.add_missing_imports).
_CLIENT_CLASSES = ("OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI")
_V1_ERRORS = sorted(
    {new for new, _, _ in ERROR_CLASSES.values() if new}
    | {"APIStatusError", "NotFoundError", "UnprocessableEntityError", "ConflictError"}
)
KNOWN_IMPORTS: dict[str, str] = {
    **{name: f"from openai import {name}" for name in (*_CLIENT_CLASSES, *_V1_ERRORS)},
    "openai": "import openai",
    "httpx": "import httpx",
}

# Object-shaped example responses for mocks, by the 1.x call they stand in for.
MOCK_RESPONSES: dict[str, str] = {
    "chat.completions.create": (
        'SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))])'
    ),
    "completions.create": 'SimpleNamespace(choices=[SimpleNamespace(text="hello")])',
    "embeddings.create": "SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2])])",
    "moderations.create": "SimpleNamespace(results=[SimpleNamespace(flagged=False)])",
    "images.generate": 'SimpleNamespace(data=[SimpleNamespace(url="https://example.test/a.png")])',
    "audio.transcriptions.create": 'SimpleNamespace(text="hello")',
}

# Test files: dict literals shaped like a 0.x response ({"choices": [{"message"/"text": ...}]}).
MOCK_DICT_RESPONSE = (
    "1.x responses are objects: build mock responses with attribute access "
    "(e.g. SimpleNamespace), not dicts."
)
RESPONSE_DICT_KEY = "choices"
RESPONSE_CHOICE_KEYS = frozenset({"message", "text"})
