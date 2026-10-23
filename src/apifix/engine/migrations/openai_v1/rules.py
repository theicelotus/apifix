"""Rule tables for the openai 0.x -> 1.x migration. Pure data, no logic."""

from __future__ import annotations

from apifix.engine.models import CERTAIN, LIKELY, CompanionPin, Confidence

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

# Assignments to these module attributes are reported on the whole statement, with a
# suggestion that depends on the value (CONFIG_DROP / CONFIG_KEEP / CONFIG_API_BASE). 1.x keeps
# api_key / organization / base_url (not api_base) for the module-level client only.
CONFIG_ASSIGN_ATTRS = frozenset({"api_key", "organization", "api_base"})
# The variable 1.x reads itself, per module attribute: an assignment from it (no default) or of
# None can go. 0.x's OPENAI_API_BASE is not one (1.x reads OPENAI_BASE_URL).
ENV_READ_BY_V1: dict[str, str] = {
    "api_key": "OPENAI_API_KEY",
    "organization": "OPENAI_ORG_ID",
    "base_url": "OPENAI_BASE_URL",
}
CONFIG_DROP = (
    "1.x reads {env} itself: this line can go (OpenAI() and the module-level client both read it)"
)
CONFIG_KEEP_PREFIX = "Never drop this value"
CONFIG_KEEP = (
    CONFIG_KEEP_PREFIX + ": keep the assignment and call the module-level client "
    "(openai.chat.completions.create(...) reads openai.{attr}), or pass it to every client: "
    "openai.OpenAI({attr}={value})"
)
CONFIG_API_BASE_PREFIX = "1.x ignores api_base"
CONFIG_API_BASE = (
    CONFIG_API_BASE_PREFIX + ": assign openai.base_url = {value} and use the module-level client, "
    "or pass openai.OpenAI(base_url={value}); never drop it"
)
CONFIG_READ = (
    "1.x keeps openai.{attr} for the module-level client only; openai.OpenAI() does not read it"
)
AZURE_CONFIG_KEEP = "; never drop the value"
# Repo-level pass (repo_findings.py): when a non-test file keeps a configured value, call sites
# use the module-level client.
MODULE_CLIENT_CALL = (
    "Use openai.{path}(...): the module-level client, which reads {attrs} (set in {where}); "
    "the response is a typed object, not a dict"
)
MODULE_CLIENT_ASYNC = (
    "Use await client.{path}(...) with client = openai.AsyncOpenAI({kwargs}) created where it "
    "is used (1.x has no module-level async client); the response is a typed object, not a dict"
)
MODULE_CLIENT_MOCK = (
    'patch "openai.{path}" (the code uses the module-level client; the test needs '
    "OPENAI_API_KEY or openai.api_key set)"
)
CONFIG_CALLS_ELSEWHERE = ". The API calls are in {files}: {keep}"

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
PIN_SPEC = ">=1.0,<2"  # what apifix writes for a 0.x pin
DEPENDENCY = f"Pin openai>=1.0 (e.g. openai{PIN_SPEC}) and migrate call sites"

# openai's own requirements, from the metadata of 1.109.1 (the last release before 2.0; PyPI,
# checked 2026-10-22). openai 1.0 accepted typing-extensions>=4.5, so an exact older pin makes
# pip fall back to an old 1.x, which breaks on httpx>=0.28. In files where apifix sets the
# openai pin, a pin of one of these that allows none of the range is raised to it. pydantic
# keeps the repo's major version (v1 code stays on v1). sniffio and tqdm are left out: no
# realistic pin excludes what openai needs.
COMPANION_PINS: dict[str, CompanionPin] = {
    "typing-extensions": CompanionPin("4.11", "5"),
    "anyio": CompanionPin("3.5", "5"),
    "httpx": CompanionPin("0.23", "1"),
    "pydantic": CompanionPin("1.9", "3", keep_major=True),
    "distro": CompanionPin("1.7", "2"),
    "jiter": CompanionPin("0.4", "1"),
}

# Guide parts (guide.md `<!-- part: id -->` markers) and when a job's prompt includes them.
# A selector (categories, keywords) matches a finding whose category is in `categories` (empty:
# any) and whose old_pattern or suggestion contains one of `keywords` (empty: no condition).
GUIDE_ALWAYS = frozenset({"client"})
GUIDE_FOR_TEST_FILES = frozenset({"tests"})  # when a finding is in a test file
GUIDE_SELECTORS: dict[str, tuple[tuple[frozenset[str], tuple[str, ...]], ...]] = {
    "client.async": ((frozenset(), ("AsyncOpenAI", "await ", "aiosession")),),
    "client.azure": ((frozenset(), ("Azure", "azure", "deployment_id")),),
    "client.config": ((frozenset({"client_config"}), (CONFIG_KEEP_PREFIX, "never drop it")),),
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
