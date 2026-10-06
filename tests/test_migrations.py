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
