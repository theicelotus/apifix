from unittest import mock

from assist.generate import suggest


@mock.patch(
    "openai.ChatCompletion.create",
    return_value={"choices": [{"message": {"role": "assistant", "content": "Define b"}}]},
    autospec=True,
)
def test_suggest(create):
    assert suggest("NameError: b") == "Define b"
    create.assert_called_once()
    assert create.call_args.kwargs["model"] == "gpt-3.5-turbo"


@mock.patch("assist.generate.openai.ChatCompletion.create", autospec=True)
def test_suggest_passes_the_error(create):
    create.return_value = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    suggest("ZeroDivisionError")
    (message,) = create.call_args.kwargs["messages"]
    assert "ZeroDivisionError" in message["content"]
