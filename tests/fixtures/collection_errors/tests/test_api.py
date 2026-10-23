from unittest import mock

from src.suspense.api import rate


@mock.patch("openai.ChatCompletion.create")
def test_rate(create):
    create.return_value = {"choices": [{"message": {"content": " 7\n"}}]}
    assert rate("The door creaked.") == 7
