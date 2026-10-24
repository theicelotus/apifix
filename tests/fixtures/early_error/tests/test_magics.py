from unittest import mock

from cellhelp.magics import assist


@mock.patch(
    "openai.ChatCompletion.create",
    return_value={"choices": [{"message": {"role": "assistant", "content": "superplot(df)"}}]},
)
def test_assist(create):
    assert assist("plot df") == "superplot(df)"
    create.assert_called_once()
