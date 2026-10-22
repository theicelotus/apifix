from unittest import mock

from voicebot.chat import reply


@mock.patch("openai.ChatCompletion.create")
def test_reply(create):
    create.return_value = {"choices": [{"message": {"content": "hi"}}]}
    assert reply("hello") == "hi"
