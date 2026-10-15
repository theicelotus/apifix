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
    assert '@mock.patch("app.openai.OpenAI")' in text and "## Dependencies" in text
    assert len(text) < len(get_migration("openai-v1").guide) * 0.6
    assert "\n\n## Resources" in text and "\n\n\n" not in text.replace("\n\n\n# 1.x", "")


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
        (_finding("response_access", 'chunk["choices"][0]["delta"]'), "responses.streaming"),
        (_finding("dependency", "openai==0.28.1"), "dependencies"),
        (_finding("api_call", file="tests/test_app.py"), "tests"),
    ],
)
def test_guide_parts_follow_the_findings(finding, part):
    from apifix.engine.migrations.openai_v1.guide_parts import selected_parts

    assert part in selected_parts([finding])
    assert part.split(".")[0] in selected_parts([finding])  # its section, for the heading
    assert part not in selected_parts([_finding("dependency")]) or part == "dependencies"


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
