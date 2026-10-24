"""Cell magics: %%assist asks the model about a cell, with the history as context."""

from notebook.context import build_context
from notebook.generate import generate_reply


def assist(shell, cell):
    messages = build_context(shell.history)
    reply = "".join(generate_reply(messages, cell))
    shell.history.append({"role": "assistant", "content": reply})
    return reply
