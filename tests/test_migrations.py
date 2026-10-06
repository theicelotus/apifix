import pytest

from apifix.engine.migrations import available_migrations, get_migration


def test_registry():
    assert available_migrations() == ["openai-v1"]
    assert get_migration("openai-v1").name == "openai-v1"


def test_unknown_migration():
    with pytest.raises(ValueError, match="unknown migration 'nope'.*openai-v1"):
        get_migration("nope")
