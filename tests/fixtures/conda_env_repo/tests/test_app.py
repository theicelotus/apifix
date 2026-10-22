from unittest import mock

from app import summarize


@mock.patch("openai.Completion.create")
def test_summarize(create):
    create.return_value = {"choices": [{"text": "short"}]}
    assert summarize("long text") == "short"
