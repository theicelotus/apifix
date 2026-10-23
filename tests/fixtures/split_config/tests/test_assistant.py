"""Tests for the assistant, with the chat API mocked."""

import unittest
from unittest import mock

from voicebot.assistant import Assistant


class AssistantTest(unittest.TestCase):
    @mock.patch("openai.ChatCompletion.create")
    def test_reply_is_cleaned(self, create):
        create.return_value = {
            "model": "gpt-3.5-turbo",
            "choices": [{"message": {"content": " Hello \n  there "}}],
        }
        self.assertEqual(Assistant().reply("hi"), "Hello there")

    @mock.patch("openai.ChatCompletion.create")
    def test_constructing_makes_no_call(self, create):
        Assistant(voice="bright")
        create.assert_not_called()

    def test_describe(self):
        self.assertEqual(Assistant("calm").describe(), "assistant with a calm voice")


if __name__ == "__main__":
    unittest.main()
