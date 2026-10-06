from unittest import mock

from app import ask


def completion(text):
    return {"choices": [{"message": {"content": text}}]}


@mock.patch("openai.ChatCompletion.create")
def test_ask(create):
    create.return_value = completion(" hello ")
    assert ask("hi") == "hello"
