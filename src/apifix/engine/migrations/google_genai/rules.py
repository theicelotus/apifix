"""Rule tables for the google-generativeai -> google-genai migration. Pure data, no logic."""

from __future__ import annotations

from apifix.engine.models import CERTAIN, LIKELY, ClientKey, CompanionPin, Confidence

CLIENT = 'client = genai.Client(api_key="...")'
ASYNC_NOTE = "client.aio.models / client.aio.chats mirror the sync API; await each call"

# google.generativeai call -> the new client call, kept as (path, suggestion template).
GENERATE_CONTENT = "client.models.generate_content(model=..., contents=..., config=...)"
GENERATE_CONTENT_STREAM = (
    "client.models.generate_content_stream(model=..., contents=..., config=...)"
)
COUNT_TOKENS = "client.models.count_tokens(model=..., contents=...)"
START_CHAT = "client.chats.create(model=..., config=..., history=...)"
SEND_MESSAGE = "chat.send_message(...)"
SEND_MESSAGE_STREAM = "chat.send_message_stream(...)"
EMBED_CONTENT = "client.models.embed_content(model=..., contents=..., config=...)"
LIST_MODELS = "client.models.list()"
GET_MODEL = "client.models.get(model=...)"

# genai.<name> -> (category, suggestion, confidence) for a plain module-level call/name, where
# the call's own shape does not change the suggestion (unlike generate_content, which depends
# on whether it is tracked to a model/chat instance).
MODULE_CALLS: dict[str, tuple[str, str, Confidence]] = {
    "configure": (
        "client_config",
        f"Use {CLIENT}; build it where it is used, not at import",
        CERTAIN,
    ),
    "GenerativeModel": (
        "api_call",
        "No model object in the new SDK: keep the model name, move every setting "
        "(system_instruction, generation_config, safety_settings, tools, tool_config) into "
        "types.GenerateContentConfig passed on each call",
        CERTAIN,
    ),
    "embed_content": (
        "api_call",
        f"Use {EMBED_CONTENT}; the result is an object, not a dict",
        CERTAIN,
    ),
    "embed_content_async": (
        "api_call",
        f"Use await client.aio.models.embed_content(...); {ASYNC_NOTE}",
        CERTAIN,
    ),
    "list_models": ("api_call", f"Use {LIST_MODELS}", CERTAIN),
    "get_model": ("api_call", f"Use {GET_MODEL}", CERTAIN),
    "get_base_model": ("api_call", f"Use {GET_MODEL}", CERTAIN),
    "GenerationConfig": (
        "api_call",
        "Its fields go into types.GenerateContentConfig(...)",
        CERTAIN,
    ),
    "upload_file": (
        "api_call",
        "Files API out of scope: client.files.upload(file=...) differs (state polling, file "
        "objects); leave for a manual follow-up",
        LIKELY,
    ),
    "get_file": (
        "api_call",
        "Files API out of scope: client.files.get(...); leave for a manual follow-up",
        LIKELY,
    ),
    "list_files": (
        "api_call",
        "Files API out of scope: client.files.list(...); leave for a manual follow-up",
        LIKELY,
    ),
    "delete_file": (
        "api_call",
        "Files API out of scope: client.files.delete(...); leave for a manual follow-up",
        LIKELY,
    ),
    "create_tuned_model": ("api_call", "Tuning out of scope; no equivalent migrated here", LIKELY),
    "list_operations": ("api_call", "Operations out of scope; no equivalent migrated here", LIKELY),
    "get_operation": ("api_call", "Operations out of scope; no equivalent migrated here", LIKELY),
    "generate_text": (
        "api_call",
        "PaLM-era API removed; its models are shut down, no equivalent",
        CERTAIN,
    ),
    "chat": ("api_call", "PaLM-era API removed; its models are shut down, no equivalent", CERTAIN),
    "generate_embeddings": (
        "api_call",
        "PaLM-era API removed; its models are shut down, no equivalent",
        CERTAIN,
    ),
}

# Methods on a tracked GenerativeModel instance -> (category, suggestion, confidence).
MODEL_METHODS: dict[str, tuple[str, str, Confidence]] = {
    "generate_content": (
        "api_call",
        f"Use {GENERATE_CONTENT}; the response is a typed object, not a dict",
        CERTAIN,
    ),
    "generate_content_async": (
        "api_call",
        f"Use await client.aio.models.generate_content(...); {ASYNC_NOTE}",
        CERTAIN,
    ),
    "count_tokens": ("api_call", f"Use {COUNT_TOKENS}", CERTAIN),
    "count_tokens_async": (
        "api_call",
        f"Use await client.aio.models.count_tokens(...); {ASYNC_NOTE}",
        CERTAIN,
    ),
    "start_chat": ("api_call", f"Use {START_CHAT}", CERTAIN),
}

# Methods on a tracked ChatSession instance -> (category, suggestion, confidence).
CHAT_METHODS: dict[str, tuple[str, str, Confidence]] = {
    "send_message": (
        "api_call",
        f"Use {SEND_MESSAGE} (client.chats.create(...) built it); the response is a typed "
        "object, not a dict",
        CERTAIN,
    ),
    "send_message_async": ("api_call", f"Use await chat.send_message(...); {ASYNC_NOTE}", CERTAIN),
}
# response_access on a tracked ChatSession instance, not a call.
CHAT_ATTRS: dict[str, tuple[str, str, Confidence]] = {
    "history": ("response_access", "Use chat.get_history() (a method, not a property)", CERTAIN),
    "rewind": (
        "response_access",
        "ChatSession.rewind() has no equivalent; manage history yourself",
        LIKELY,
    ),
    "last": (
        "response_access",
        "ChatSession.last has no equivalent; keep the last response yourself",
        LIKELY,
    ),
}

# Removed error classes (google.generativeai.types.*) -> suggestion.
ERROR_CLASSES: dict[str, str] = {
    "BlockedPromptException": (
        "Removed: check response.prompt_feedback.block_reason instead of catching an exception"
    ),
    "StopCandidateException": (
        "Removed: check candidates[0].finish_reason instead of catching an exception"
    ),
    "IncompleteIterationError": "Removed: no equivalent (the new stream iterator has no resolve())",
    "BrokenResponseError": "Removed: no equivalent",
}

# google.api_core.exceptions.<old> -> (google.genai.errors name, note, confidence). The new SDK
# never raises google.api_core errors itself (it is not even a dependency); see also
# COMPANION_MODULES for files that keep google.api_core for another Google client.
API_CORE_ERRORS: dict[str, tuple[str, str]] = {
    "ResourceExhausted": ("ClientError", "429; check e.code == 429"),
    "InvalidArgument": ("ClientError", "400"),
    "PermissionDenied": ("ClientError", "403"),
    "Unauthenticated": ("ClientError", "401"),
    "NotFound": ("ClientError", "404"),
    "FailedPrecondition": ("ClientError", "400"),
    "DeadlineExceeded": ("ServerError", "504"),
    "ServiceUnavailable": ("ServerError", "503"),
    "InternalServerError": ("ServerError", "500"),
    "TooManyRequests": ("ClientError", "429; check e.code == 429"),
    "BadRequest": ("ClientError", "400"),
    "Forbidden": ("ClientError", "403"),
    "Conflict": ("ClientError", "409"),
    "GatewayTimeout": ("ServerError", "504"),
    "BadGateway": ("ServerError", "502"),
    "GoogleAPICallError": ("APIError", "the common base class, 4xx and 5xx alike"),
    "GoogleAPIError": ("APIError", "the common base class, 4xx and 5xx alike"),
}
# An exception under google.api_core.exceptions with no entry above.
API_CORE_OTHER = ("APIError", "ClientError for a 4xx status, ServerError for a 5xx one")
# Every google.api_core finding's suggestion contains this text: repo_findings.py tells them
# apart by it, then decides from the repo whether the import survives.
API_CORE_MARK = "google-api-core is no longer installed with google-genai"
API_CORE_NOTE = (
    "google.genai.errors.{new} (" + API_CORE_MARK + "; {note}; to build one in a test: "
    "errors.{new}(code, response_json))"
)
API_CORE_IMPORT = (
    API_CORE_MARK + " (it keeps working only if the repo declares google-api-core): use "
    "`from google.genai import errors` (ClientError / ServerError / APIError) in place of "
    "google.api_core.exceptions, and drop this import if nothing else in the file needs "
    "google.api_core"
)
# Distributions that depend on google-api-core: declaring one keeps it installed.
API_CORE_DEPENDENTS_PREFIXES = ("google-cloud-",)
API_CORE_DEPENDENTS = frozenset({"google-api-core", "google-api-python-client"})
GEMINI_DISTRIBUTIONS = frozenset({"google-generativeai", "google-genai"})

# A patch target that goes through a removed module-level name of the old SDK.
PATCH_TARGET = (
    "String patch target `{target}` names `{name}`, which the new SDK's module does not have: "
    "patch `{prefix}.Client` instead ({what}) and set its models / chats return value"
)

RESPONSE_TEXT_NONE = (
    "`.text` returns None instead of raising ValueError when there is no text; a "
    "try/except ValueError around it should become a None check"
)
EMBEDDING_DICT_ACCESS = (
    "Use result.embeddings[0].values (batch: [e.values for e in result.embeddings]); "
    'embed_content no longer returns a dict ({"embedding": [...]})'
)
RESOLVE_REMOVED = "response.resolve() is gone: iterate the stream to completion instead"
TO_DICT_RENAMED = "response.to_dict() is gone: use response.model_dump()"
SUPPORTED_ACTIONS_RENAMED = "Model.supported_generation_methods is renamed supported_actions"

STREAM_CALL_NOTE = (
    "; it streams: use client.models.generate_content_stream(...) (chunk.text may be None, so "
    'use `chunk.text or ""`)'
)
CHAT_STREAM_CALL_NOTE = (
    "; it streams: use chat.send_message_stream(...) (chunk.text may be None, so use `chunk.text "
    'or ""`)'
)

PROTOS_MODULE = (
    "genai.protos / google.ai.generativelanguage have no equivalent for Gemini calls; use "
    "types.* instead"
)

VERTEX_NOTE = (
    "Vertex AI mode (vertexai=True / GOOGLE_GENAI_USE_VERTEXAI) is out of scope for this "
    "migration; leave it alone"
)

PACKAGE = "google-genai"
SOURCE_PACKAGE = "google-generativeai"
# The API's first release (used by already_migrated.py and the "does this declaration still
# allow the old SDK" check); the pin's own floor (>=1.19.0) is stricter, for GEMINI_API_KEY
# fallback parity, and is only what apifix writes once a finding is already flagged.
FIRST_VERSION = "1.0.0"
PIN_SPEC = ">=1.19.0,<3"
PIN_REASONS: dict[str, str] = {
    ">=1.19.0": "the first release that reads GEMINI_API_KEY, which the old SDK read first",
    "<3": (
        "2.x kept generate_content's API (2.0 broke only Interactions); a 3.0 has not been checked"
    ),
}
DEPENDENCY = f"Pin google-genai{PIN_SPEC} and migrate call sites (google-generativeai is removed)"
LOOSE_DEPENDENCY = (
    f"Pin google-genai{PIN_SPEC}: {{what}}, so a lockfile or an existing environment can "
    "install an older release, which the migrated code cannot use"
)
SOURCE_DEPENDENCY = (
    "google-generativeai is no longer supported (support ended 2025-11-30, its repository is "
    f"archived); replace it with google-genai{PIN_SPEC}"
)

CLIENT_KEY = ClientKey(
    envs=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    keyword="api_key",
    classes=frozenset({"Client"}),
    resources="",
)
CLIENT_CLASSES = frozenset({"Client"})
CLIENT_RESOURCES = frozenset(
    {"models", "chats", "aio", "files", "caches", "batches", "tunings", "operations"}
)
KNOWN_IMPORTS: dict[str, str] = {
    "genai": "from google import genai",
    "errors": "from google.genai import errors",
}
MOCK_RESPONSES: dict[str, str] = {
    "models.generate_content": 'MagicMock(text="hello", candidates=[MagicMock()])',
    "models.embed_content": "MagicMock(embeddings=[MagicMock(values=[0.1, 0.2])])",
    "models.count_tokens": "MagicMock(total_tokens=3)",
    "chats.create": 'MagicMock(send_message=MagicMock(return_value=MagicMock(text="hello")))',
}
KWARG_RENAMES: dict[str, str] = {
    "model_name": "model",
    "content": "contents",
    "generation_config": "config",
}
COMPANION_PINS: dict[str, CompanionPin] = {}

# Guide parts (guide.md `<!-- part: id -->` markers) and when a job's prompt includes them; the
# same mechanism as openai_v1 (guide_parts.selected_parts).
GUIDE_ALWAYS = frozenset({"client"})
GUIDE_FOR_TEST_FILES = frozenset({"tests"})
GUIDE_PER_FILE = frozenset({"streaming"})
GUIDE_SELECTORS: dict[str, tuple[tuple[frozenset[str], tuple[str, ...]], ...]] = {
    "client.async": ((frozenset(), ("_async", "await", "aio")),),
    "models": ((frozenset({"api_call"}), ("GenerativeModel", "generate_content", "count_tokens")),),
    "chat": (
        (
            frozenset({"api_call", "response_access"}),
            ("start_chat", "send_message", "ChatSession", "get_history"),
        ),
    ),
    "embeddings": ((frozenset({"api_call", "response_access"}), ("embed_content",)),),
    "responses": ((frozenset({"response_access"}), ()),),
    "streaming": ((frozenset({"api_call", "response_access"}), ("stream", "delta")),),
    "safety": ((frozenset(), ("safety_settings", "HarmCategory", "SafetySetting")),),
    "errors": ((frozenset({"error_class"}), ()),),
    "dependencies": ((frozenset({"dependency"}), ()),),
}
