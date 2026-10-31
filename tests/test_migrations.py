import pytest

from apifix.engine.migrations import available_migrations, get_migration


def test_registry():
    assert available_migrations() == ["openai-v1"]
    assert get_migration("openai-v1").name == "openai-v1"


def test_unknown_migration():
    with pytest.raises(ValueError, match="unknown migration 'nope'.*openai-v1"):
        get_migration("nope")


def test_openai_guide_covers_every_rule():
    """The agent's prompt guide must not drift from the scanner's rule tables."""
    from apifix.engine.migrations.openai_v1 import rules

    guide = get_migration("openai-v1").guide
    for name in [*rules.RESOURCES, *rules.REMOVED_RESOURCES, *rules.ERROR_CLASSES]:
        assert f"openai.{name}" in guide or f"openai.error.{name}" in guide or name in guide, name
    for path in rules.RESOURCES.values():
        assert f"client.{path}" in guide, path
    for module in rules.REMOVED_MODULES:
        assert f"openai.{module}" in guide, module


# --- guide parts --------------------------------------------------------------------------------


def _finding(category, old="x", suggestion="y", file="app.py"):
    from apifix.engine.models import Finding

    return Finding(file, 1, 0, 1, 1, category, old, suggestion, "certain")


def test_every_guide_part_has_a_selector_and_the_full_guide_is_all_parts():
    from apifix.engine.migrations.guide import TITLE, full_guide, render_parts
    from apifix.engine.migrations.openai_v1 import guide_parts, rules

    ids = [part for part, _ in guide_parts.PARTS]
    assert ids[0] == TITLE and len(set(ids)) == len(ids)
    selected = rules.GUIDE_ALWAYS | rules.GUIDE_FOR_TEST_FILES | set(rules.GUIDE_SELECTORS)
    assert set(ids[1:]) == selected  # no part can silently never be sent
    full = get_migration("openai-v1").guide
    assert "<!--" not in full and full == full_guide(guide_parts.GUIDE_SOURCE)
    assert render_parts(guide_parts.PARTS, set(ids)) == full.rstrip() + "\n"


def test_job_guide_for_the_live_run():
    from apifix.engine.models import Finding

    findings = [
        Finding(
            "app.py",
            5,
            16,
            5,
            44,
            "api_call",
            "openai.ChatCompletion.create",
            "Use client.chat.completions.create(...) with client = openai.OpenAI(); the "
            "response is a typed object, not a dict",
            "certain",
        ),
        Finding(
            "app.py",
            9,
            12,
            9,
            56,
            "response_access",
            'response["choices"][0]["message"]["content"]',
            "1.x returns typed objects",
            "likely",
        ),
        Finding("requirements.txt", 1, 0, 1, 14, "dependency", "openai==0.28.1", "Pin", "certain"),
        Finding(
            "tests/test_app.py",
            10,
            12,
            10,
            42,
            "api_call",
            '"openai.ChatCompletion.create"',
            "Use client.chat.completions.create",
            "likely",
        ),
    ]
    text, parts = get_migration("openai-v1").job_guide(findings)
    assert parts == ["client", "resources", "responses", "tests", "dependencies"]
    assert "| `openai.ChatCompletion.create` / `acreate` |" in text
    for gone in (
        "openai.Embedding",
        "## Errors",
        "Removed with no direct equivalent",
        "Azure",
        "Streaming",
        "AsyncOpenAI",
    ):
        assert gone not in text, gone
    assert '@mock.patch("yourpkg.module.openai.OpenAI")' in text and "## Dependencies" in text
    assert len(text) < len(get_migration("openai-v1").guide) * 0.6
    assert "\n\n## Resources" in text and "\n\n\n## " not in text  # sections joined cleanly


@pytest.mark.parametrize(
    ("finding", "part"),
    [
        (_finding("error_class", "openai.error.RateLimitError"), "errors"),
        (_finding("import", "RateLimitError", "from openai import RateLimitError"), "errors"),
        (_finding("removed_module", "openai.embeddings_utils"), "resources.removed"),
        (_finding("api_call", "openai.FineTune.create"), "resources.removed"),
        (
            _finding("api_call", "openai.ChatCompletion.acreate", "await client... AsyncOpenAI()"),
            "client.async",
        ),
        (_finding("client_config", "openai.api_type", "use openai.AzureOpenAI"), "client.azure"),
        (_finding("client_config", "openai.api_key"), "client.kwargs"),
        (_finding("api_call", "request_timeout=30"), "client.kwargs"),
        (_finding("response_access", 'chunk["choices"][0]["delta"]'), "streaming"),
        (_finding("dependency", "openai==0.28.1"), "dependencies"),
        (_finding("api_call", file="tests/test_app.py"), "tests"),
    ],
)
def test_guide_parts_follow_the_findings(finding, part):
    from apifix.engine.migrations.openai_v1.guide_parts import selected_parts

    assert part in selected_parts([finding])
    assert part.split(".")[0] in selected_parts([finding])  # its section, for the heading
    assert part not in selected_parts([_finding("dependency")]) or part == "dependencies"


def test_client_config_part_only_for_values_that_must_be_kept():
    from apifix.engine.migrations.openai_v1 import rules

    mig = get_migration("openai-v1")
    keep = _finding(
        "client_config",
        "openai.api_key = key",
        rules.CONFIG_KEEP.format(attr="api_key", value="key"),
    )
    base = _finding(
        "client_config", "openai.api_base = url", rules.CONFIG_API_BASE.format(value="url")
    )
    env = _finding(
        "client_config",
        'openai.api_key = os.getenv("OPENAI_API_KEY")',
        rules.CONFIG_DROP.format(env="OPENAI_API_KEY"),
    )
    for finding in (keep, base):
        text, parts = mig.job_guide([finding])
        assert "client.config" in parts
        assert "**Never drop a configured value**" in text
        assert "set a key from somewhere else (see below)." in text
    text, parts = mig.job_guide([env])
    assert "client.config" not in parts and "Never drop" not in text
    assert "set a key from somewhere else." in text


def test_module_client_failure_hint():
    (_, hint), *_ = get_migration("openai-v1").failure_hints
    assert 'openai.api_key = "test"' in hint and "module-level client" in hint


def test_autospec_client_failure_hint():
    hints = dict(get_migration("openai-v1").failure_hints)
    hint = hints["Mock object has no attribute 'completions'"]
    assert "autospec=True" in hint and "cached properties" in hint
    assert 'patch("yourpkg.module.OpenAI")' in hint


def test_resources_table_keeps_only_used_rows():
    mig = get_migration("openai-v1")
    text, _ = mig.job_guide(
        [_finding("api_call", "oai.Moderation.create", "Use client.moderations.create(...)")]
    )
    assert "| `openai.Moderation.create` |" in text
    assert "openai.ChatCompletion" not in text.split("## Resources")[1].split("##")[0]
    # Completion is not ChatCompletion
    text, _ = mig.job_guide([_finding("api_call", "openai.ChatCompletion.create")])
    assert "| `openai.Completion.create` |" not in text
    # only a removed resource: the section without the table
    text, parts = mig.job_guide([_finding("removed_module", "openai.embeddings_utils")])
    assert "resources.removed" in parts and "| 0.x | 1.x |\n|---|---|\n| `openai.Chat" not in text
    assert "## Resources\n\nRemoved with no direct equivalent:" in text


# --- known imports, mock responses, guide advice -------------------------------------------------


def test_known_imports_cover_the_1x_names():
    from apifix.engine.migrations.openai_v1 import rules

    known = get_migration("openai-v1").known_imports
    for new, _, _ in rules.ERROR_CLASSES.values():
        if new:
            assert known[new] == f"from openai import {new}"
    for name in ("OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI", "NotFoundError"):
        assert known[name] == f"from openai import {name}"
    assert (known["openai"], known["httpx"]) == ("import openai", "import httpx")


def test_mock_responses_are_object_shaped_and_name_real_resources():
    from apifix.engine.migrations.openai_v1 import rules

    paths = set(rules.RESOURCES.values()) | {"audio.transcriptions"}
    for call, example in get_migration("openai-v1").mock_responses.items():
        assert call.rsplit(".", 1)[0] in paths, call
        assert example.startswith("SimpleNamespace(") and "{" not in example, call


def test_guide_advice_on_keys_and_mock_data():
    guide = get_migration("openai-v1").guide
    client = guide.split("## Client")[1].split("## Resources")[0]
    v1_example = client.split("# 1.x")[1].split("```")[0]
    assert "os.getenv" not in v1_example and "import os" not in v1_example
    assert 'do not add\n  `api_key=os.getenv("OPENAI_API_KEY")` or `import os` for it' in client
    tests = guide.split("## Tests")[1]
    assert "Mock responses must be objects, not dicts" in tests
    assert (
        '    message = SimpleNamespace(role="assistant", content=text)\n'
        "    return SimpleNamespace(choices=[SimpleNamespace(message=message)])"
    ) in tests


def test_guide_says_to_drop_autospec_on_a_client_class():
    from apifix.engine.migrations.openai_v1 import rules

    tests = get_migration("openai-v1").guide.split("## Tests")[1].split("## Dependencies")[0]
    assert "- Drop `autospec=True` when patching a client class" in tests
    assert "an autospecced 1.x client has no `chat.completions`" in tests
    # the client-class mock example says so where it patches the class
    example = tests.split("# 1.x, client created inside the function")[1].split("# 1.x,")[0]
    assert "no autospec=True on a client class" in example
    assert {"OpenAI", "AsyncOpenAI", "AzureOpenAI", "AsyncAzureOpenAI"} == rules.CLIENT_CLASSES
    assert {"chat", "completions", "embeddings"} <= rules.CLIENT_RESOURCES
    assert all(rules.KNOWN_IMPORTS[c] == f"from openai import {c}" for c in rules.CLIENT_CLASSES)


def test_tests_part_for_a_test_file_recognised_by_content(tmp_path):
    from apifix.engine.migrations.openai_v1.guide_parts import selected_parts

    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "checks.py").write_text(
        "import unittest\n\n\nclass ServerChecks(unittest.TestCase):\n    pass\n"
    )
    finding = _finding("api_call", file="svc/checks.py")
    assert "tests" not in selected_parts([finding])  # by name alone it is not a test file
    assert "tests" in selected_parts([finding], tmp_path)
    _, parts = get_migration("openai-v1").job_guide([finding], tmp_path)
    assert "tests" in parts


def test_openai_smoke_check():
    smoke = get_migration("openai-v1").smoke_check
    assert smoke.distribution == "openai"
    compile(smoke.code, "smoke_check.py", "exec")
    assert "openai.OpenAI(api_key=" in smoke.code and "sk-apifix" in smoke.code


def test_httpx_proxies_hint():
    hints = dict(get_migration("openai-v1").failure_hints)
    hint = hints["unexpected keyword argument 'proxies'"]
    assert "openai>=1.55.3" in hint and "httpx<0.28" in hint and "typing_extensions" in hint


def test_guide_prefers_creating_the_client_where_it_is_used():
    guide = get_migration("openai-v1").guide
    client = guide.split("## Client")[1].split("##")[0]
    assert "Prefer creating the client where it is used" in client
    assert "def ask(msgs):\n    client = OpenAI(" in client
    deps = guide.split("## Dependencies")[1]
    assert "typing_extensions==4.5.0" in deps and "httpx>=0.28" in deps


@pytest.mark.parametrize(
    ("category", "old", "new_code", "bullet"),
    [
        (
            "api_call",
            "openai.ChatCompletion.create",
            "client.chat.completions.create(",
            "`openai.ChatCompletion.create` → `client.chat.completions.create`",
        ),
        (
            "api_call",
            '"openai.ChatCompletion.create"',
            'mock.patch("x.client.chat.completions.create")',
            "`openai.ChatCompletion.create` → `client.chat.completions.create`",
        ),
        (
            "api_call",
            "openai.ChatCompletion.acreate",
            "await client.chat.completions.create(",
            "`openai.ChatCompletion.acreate` → `await client.chat.completions.create`",
        ),
        ("api_call", "openai.Image.create", "client.images.generate(", "`client.images.generate`"),
        ("api_call", "engine=self.model", "model=self.model", "`engine=` → `model=`"),
        ("api_call", "openai.ChatCompletion.create", "something_else()", None),  # not in the diff
        ("api_call", "engine=self.model", "self.model", None),
        (
            "response_access",
            'resp["choices"]',
            "resp.choices",
            'dict responses (`resp["choices"]`)',
        ),
        ("response_access", 'resp["choices"]', 'resp["choices"]', None),  # still there
        ("error_class", "RateLimitError", "openai.RateLimitError", "`openai.RateLimitError`"),
        ("import", "InvalidRequestError", "BadRequestError", "→ `openai.BadRequestError`"),
        ("import", "TryAgain", "pass", None),  # removed: no replacement
        ("api_call", "openai.FineTune.create", "client.x.create", None),  # removed API
        ("import", "numpy", "numpy", None),
        ("dependency", "openai==0.27.4", "openai>=1", None),
    ],
)
def test_openai_pr_change_bullets(category, old, new_code, bullet):
    got = get_migration("openai-v1").pr_change(category, old, new_code, new_code)
    assert (bullet in got) if bullet else got is None


KEY = 'openai.api_key = os.getenv("OPENAI_API_KEY")'


@pytest.mark.parametrize(
    ("old", "new_code", "bullet"),
    [
        (KEY, "", f"`{KEY}` removed: `OpenAI()` reads `OPENAI_API_KEY` itself"),
        (KEY, "client = OpenAI()", f"`{KEY}` removed: `OpenAI()` reads `OPENAI_API_KEY` itself"),
        (
            KEY,
            'client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=5)',
            f'`{KEY}` → client argument `api_key=os.getenv("OPENAI_API_KEY")`',
        ),
        ("openai.api_key = key", "client = OpenAI()", "`openai.api_key = key` removed"),
        (
            'openai.api_base = "http://x"',
            'client = OpenAI(base_url="http://x")',
            '`openai.api_base = "http://x"` → client argument `base_url="http://x"`',
        ),
        (
            'openai.api_base = "http://x"',
            'openai.base_url = "http://x"',
            '`openai.api_base = "http://x"` → `openai.base_url = "http://x"`',
        ),
        (  # a commented-out line is not a replacement
            KEY,
            f"# {KEY}  # Removed: api_key=...",
            f"`{KEY}` removed: `OpenAI()` reads `OPENAI_API_KEY` itself",
        ),
        ("openai.something = 1", "", None),
    ],
)
def test_openai_config_bullets_name_only_what_the_diff_did(old, new_code, bullet):
    got = get_migration("openai-v1").pr_change("client_config", old, new_code, new_code)
    assert got == bullet


def test_openai_pin_reasons_name_each_clause_of_the_pin_spec():
    mig = get_migration("openai-v1")
    assert set(mig.pin_reasons) == set(mig.pin_spec.split(","))


def test_guide_says_to_delete_replaced_code_not_comment_it_out():
    client = get_migration("openai-v1").guide.split("## Client")[1].split("##")[0]
    assert "Delete code you replace" in client and "never leave" in client
    assert "as a comment" in client


def test_guide_says_to_change_only_what_the_migration_needs():
    client = get_migration("openai-v1").guide.split("## Client")[1].split("##")[0]
    assert "Change only what the migration needs" in client
    assert "bare `except:` stays `except:`" in client and "formatting" in client
