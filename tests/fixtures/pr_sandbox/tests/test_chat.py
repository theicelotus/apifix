from unittest import mock

import openai
import pytest

from supportbot import chat


def reply(text, tokens=7):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"total_tokens": tokens},
    }


@mock.patch("openai.ChatCompletion.create")
def test_answer_returns_the_stripped_reply(create):
    create.return_value = reply("  Try resetting it.  ")
    assert chat.answer("I forgot my password", []) == "Try resetting it."


@mock.patch("openai.ChatCompletion.create")
def test_answer_sends_the_system_prompt_history_and_question(create):
    create.return_value = reply("ok")
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    chat.answer("Where is my order?", history)
    kwargs = create.call_args.kwargs
    assert kwargs["model"] == "gpt-3.5-turbo"
    assert kwargs["messages"][0] == {"role": "system", "content": chat.SYSTEM_PROMPT}
    assert kwargs["messages"][1:3] == history
    assert kwargs["messages"][-1] == {"role": "user", "content": "Where is my order?"}


@mock.patch("time.sleep")
@mock.patch("openai.ChatCompletion.create")
def test_answer_retries_after_a_rate_limit(create, sleep):
    create.side_effect = [openai.error.RateLimitError("slow down"), reply("done")]
    assert chat.answer("hello", []) == "done"
    sleep.assert_called_once_with(1)


@mock.patch("time.sleep")
@mock.patch("openai.ChatCompletion.create")
def test_answer_gives_up_after_three_rate_limits(create, sleep):
    create.side_effect = openai.error.RateLimitError("slow down")
    with pytest.raises(RuntimeError, match="rate limited"):
        chat.answer("hello", [])
    assert create.call_count == 3
