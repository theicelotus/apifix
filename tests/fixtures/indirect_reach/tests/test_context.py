from unittest.mock import patch

from notebook.context import build_context


@patch(
    "openai.ChatCompletion.create",
    return_value={"choices": [{"message": {"role": "assistant", "content": "plot(df)"}}]},
)
def test_assist_adds_the_reply_to_the_context(create, shell):
    shell.run_cell("a = 1")
    shell.run_cell("%%assist\nMake a plot")

    context = build_context(shell.history)

    assert context[-1] == {"role": "assistant", "content": "plot(df)"}
    assert create.call_count == 1


def test_empty_entries_are_left_out():
    assert build_context([{"role": "user", "content": ""}]) == []
