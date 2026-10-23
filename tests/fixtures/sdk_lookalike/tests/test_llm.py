from src.storyteller.llm import generate

SETTINGS = {"provider": "openai", "model": "gpt-3.5-turbo", "max_tokens": 50}


def test_openai_reply(mock_openai, reply):
    assert generate([{"role": "user", "content": "Go on."}], SETTINGS) == reply


def test_acme_reply(mock_acme):
    settings = {"provider": "acme", "model": "acme-1", "api_key": "test"}
    generate([{"role": "user", "content": "Go on."}], settings)
    mock_acme.chat.completions.create.assert_called_once()
