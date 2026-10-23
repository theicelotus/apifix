import unittest
from unittest.mock import patch

from src.topics.gen import generate_topics

RESPONSE = {"choices": [{"message": {"role": "assistant", "content": "A\nB"}}]}


class TestGenerateTopics(unittest.TestCase):
    @patch("src.topics.gen.openai.ChatCompletion.create")
    def test_topics(self, create):
        create.return_value = RESPONSE
        self.assertEqual(generate_topics("rivers"), ["A", "B"])
