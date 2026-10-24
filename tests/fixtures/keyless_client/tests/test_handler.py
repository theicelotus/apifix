from unittest import mock

from helper.handler import on_error


@mock.patch(
    "openai.ChatCompletion.create",
    return_value={"choices": [{"message": {"role": "assistant", "content": "Define b"}}]},
    autospec=True,
)
def test_on_error(create):
    shown = on_error("NameError: b")
    create.assert_called_once()
    args, kwargs = create.call_args
    assert args == ()
    assert kwargs["model"] == "gpt-3.5-turbo"
    assert shown == "Suggestion: Define b"


@mock.patch("builtins.print")
@mock.patch("helper.handler.suggest", side_effect=RuntimeError("boom"))
def test_on_error_survives_a_failure(suggest, print):
    assert on_error("x") == ""
