from unittest import mock

from chatpad.generate import generate_reply, summarize


def completion(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@mock.patch("openai.ChatCompletion.create")
def test_generate_reply(create):
    create.return_value = completion("hello")
    assert list(generate_reply([], "hi")) == ["hello"]


@mock.patch("openai.ChatCompletion.create")
def test_summarize(create):
    create.return_value = completion("short")
    assert summarize("a long text") == "short"
