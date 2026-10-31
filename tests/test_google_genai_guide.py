"""Guide-part selection tests for the google-generativeai -> google-genai migration."""

from __future__ import annotations

from apifix.engine.migrations import get_migration
from apifix.engine.migrations.google_genai import guide_parts
from apifix.engine.models import CERTAIN, Finding

MIGRATION = get_migration("google-genai")


def _finding(category: str, old: str, suggestion: str = "") -> Finding:
    return Finding(
        file="app.py",
        line=1,
        col=1,
        end_line=1,
        end_col=1,
        category=category,  # type: ignore[arg-type]
        old_pattern=old,
        suggestion=suggestion,
        confidence=CERTAIN,
    )


def test_client_part_is_always_included():
    text, parts = MIGRATION.job_guide([_finding("import", "import google.generativeai as genai")])
    assert "client" in parts
    assert "## Client" in text


def test_every_guide_part_has_a_selector_or_is_always_sent():
    selected = set(guide_parts.rules.GUIDE_SELECTORS) | guide_parts.rules.GUIDE_ALWAYS
    all_parts = {part for part, _ in guide_parts.PARTS if part}
    assert all_parts <= selected | {"tests"}  # tests: selected by file content, not a category


def test_streaming_part_is_selected_for_a_streaming_finding():
    finding = _finding("api_call", "model.generate_content(x, stream=True)", "it streams")
    _, parts = MIGRATION.job_guide([finding])
    assert "streaming" in parts


def test_streaming_part_is_left_out_of_a_shared_system_prompt():
    finding = _finding("api_call", "model.generate_content(x, stream=True)", "it streams")
    _, parts = MIGRATION.job_guide([finding], shared=True)
    assert "streaming" not in parts


def test_embeddings_part_selected_for_embed_content_finding():
    finding = _finding("api_call", "genai.embed_content(model=m, content=c)", "")
    _, parts = MIGRATION.job_guide([finding])
    assert "embeddings" in parts


def test_errors_part_selected_for_error_class_finding():
    finding = _finding("error_class", "BlockedPromptException", "Removed")
    _, parts = MIGRATION.job_guide([finding])
    assert "errors" in parts


def test_dependencies_part_selected_for_dependency_finding():
    finding = _finding("dependency", "google-generativeai==0.8.4", "")
    _, parts = MIGRATION.job_guide([finding])
    assert "dependencies" in parts


def test_full_guide_has_every_section_heading():
    for heading in ("Client", "Models and calls", "Chat", "Embeddings", "Errors", "Tests"):
        assert f"## {heading}" in MIGRATION.guide
    assert "google-genai" in MIGRATION.guide


def test_guide_tells_the_model_to_keep_the_repos_signatures_and_return_shapes():
    assert "Keep the repo's own function signatures and return shapes" in guide_parts.parts_text(
        ["client"]
    )
    assert "return shapes" in get_migration("google-genai").guide
