from unittest import mock

from cellhelp.handler import on_error


@mock.patch("cellhelp.display.show")
@mock.patch(
    "openai.ChatCompletion.create",
    return_value={"choices": [{"message": {"role": "assistant", "content": "Define b"}}]},
)
def test_on_error(create, show):
    on_error("NameError: b")
    create.assert_called_once()
    show.assert_called_with("Define b")


@mock.patch("cellhelp.display.show", side_effect=TypeError("'Fun' is not allowed"))
@mock.patch("builtins.print")
@mock.patch(
    "openai.ChatCompletion.create",
    return_value={"choices": [{"message": {"role": "assistant", "content": "Define b"}}]},
)
def test_on_error_inside(create, print, show):
    on_error("NameError: b")
    assert print.call_args.args[0] == "Error while trying to provide a suggestion: "
    assert print.call_args.args[1].args[0] == "'Fun' is not allowed"
