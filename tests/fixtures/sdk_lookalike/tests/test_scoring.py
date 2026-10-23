from unittest.mock import patch

from src.storyteller.scoring import rate


def test_rate_parses_the_number():
    with patch("openai.ChatCompletion.create") as create:
        create.return_value = {"choices": [{"message": {"content": " 4 "}}]}
        assert rate("It was quiet.", {"model": "gpt-3.5-turbo"}) == 4
