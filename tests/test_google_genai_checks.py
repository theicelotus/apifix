"""Rewrite-check tests for the google-generativeai -> google-genai migration: `key`,
`settings`, `safety`, `streaming`, `patches` (Migration.rewrite_checker)."""

from __future__ import annotations

from pathlib import Path

from apifix.engine.migrations import get_migration

MIGRATION = get_migration("google-genai")


def checker(root: Path = Path(".")):
    return MIGRATION.rewrite_checker(root, [])


# --- key -----------------------------------------------------------------------------------


def test_key_error_when_the_api_key_value_is_dropped():
    original = 'genai.configure(api_key=os.environ["KEY"])\n'
    candidate = "client = genai.Client()\n"
    errors = checker()("app.py", original, candidate)["key"]
    assert errors and "never drop a key value" in errors[0]


def test_key_ok_when_the_same_expression_reaches_client():
    original = 'genai.configure(api_key=os.environ["KEY"])\n'
    candidate = 'client = genai.Client(api_key=os.environ["KEY"])\n'
    assert checker()("app.py", original, candidate)["key"] == []


def test_key_not_enforced_when_configure_took_no_explicit_key():
    original = "genai.configure()\n"
    candidate = "client = genai.Client()\n"
    assert checker()("app.py", original, candidate)["key"] == []


# --- settings --------------------------------------------------------------------------------


def test_settings_error_when_system_instruction_is_dropped():
    original = 'genai.GenerativeModel("gemini-1.5-flash", system_instruction="be terse")\n'
    candidate = (
        "client.models.generate_content(model=name, contents=c, "
        "config=types.GenerateContentConfig(temperature=0.2))\n"
    )
    errors = checker()("app.py", original, candidate)["settings"]
    assert errors and "system_instruction" in errors[0]


def test_settings_ok_when_every_keyword_survives():
    original = 'genai.GenerativeModel("gemini-1.5-flash", system_instruction="be terse")\n'
    candidate = (
        "client.models.generate_content(model=name, contents=c, "
        'config=types.GenerateContentConfig(system_instruction="be terse"))\n'
    )
    assert checker()("app.py", original, candidate)["settings"] == []


def test_settings_follows_generation_config_fields():
    original = (
        'genai.GenerativeModel("m", generation_config=genai.GenerationConfig('
        "temperature=0.2, max_output_tokens=256))\n"
    )
    candidate = (
        "client.models.generate_content(model=name, contents=c, "
        "config=types.GenerateContentConfig(temperature=0.2))\n"
    )
    errors = checker()("app.py", original, candidate)["settings"]
    assert errors and "max_output_tokens" in errors[0]


# --- safety ----------------------------------------------------------------------------------


def test_safety_error_when_a_category_is_dropped():
    original = (
        'genai.GenerativeModel("m", safety_settings='
        "{HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE})\n"
    )
    candidate = (
        "client.models.generate_content(model=name, contents=c, "
        "config=types.GenerateContentConfig())\n"
    )
    errors = checker()("app.py", original, candidate)["safety"]
    assert errors and "HARM_CATEGORY_HARASSMENT" in errors[0]


def test_safety_ok_when_the_tokens_survive():
    original = (
        'genai.GenerativeModel("m", safety_settings='
        "{HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE})\n"
    )
    candidate = (
        "config=types.GenerateContentConfig(safety_settings=[types.SafetySetting("
        'category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE")])\n'
    )
    assert checker()("app.py", original, candidate)["safety"] == []


# --- streaming -------------------------------------------------------------------------------


def test_streaming_error_on_leftover_stream_kwarg():
    candidate = "client.models.generate_content(model=m, contents=c, stream=True)\n"
    errors = checker()("app.py", "", candidate)["streaming"]
    assert errors and "generate_content_stream" in errors[0]


def test_streaming_error_on_unawaited_async_for():
    candidate = (
        "async def run():\n"
        "    async for chunk in client.aio.models.generate_content_stream(model=m, contents=c):\n"
        "        pass\n"
    )
    errors = checker()("app.py", "", candidate)["streaming"]
    assert errors and "must be awaited" in errors[0]


def test_streaming_error_on_unguarded_chunk_text():
    candidate = (
        "def run():\n"
        "    text = ''\n"
        "    for chunk in client.models.generate_content_stream(model=m, contents=c):\n"
        "        text += chunk.text\n"
    )
    errors = checker()("app.py", "", candidate)["streaming"]
    assert errors and "may be None" in errors[0]


def test_streaming_ok_with_a_none_guard():
    candidate = (
        "def run():\n"
        "    text = ''\n"
        "    for chunk in client.models.generate_content_stream(model=m, contents=c):\n"
        '        text += chunk.text or ""\n'
    )
    assert checker()("app.py", "", candidate)["streaming"] == []


# --- patches ---------------------------------------------------------------------------------


def test_patches_error_on_removed_module_function():
    candidate = (
        "def test_ask():\n"
        '    with patch("app.llm.genai.GenerativeModel") as model_cls:\n'
        "        pass\n"
    )
    errors = checker()("test_app.py", "", candidate)["patches"]
    assert errors and "app.llm.genai.Client" in errors[0]


def test_patches_error_on_autospec_client():
    candidate = (
        "def test_ask():\n"
        '    with patch("app.llm.genai.Client", autospec=True) as client_cls:\n'
        "        pass\n"
    )
    errors = checker()("test_app.py", "", candidate)["patches"]
    assert errors and "autospec" in errors[0]


def test_patches_ok_for_an_unpatched_client():
    candidate = (
        'def test_ask():\n    with patch("app.llm.genai.Client") as client_cls:\n        pass\n'
    )
    assert checker()("test_app.py", "", candidate)["patches"] == []
