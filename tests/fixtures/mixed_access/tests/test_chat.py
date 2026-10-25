import unittest
from unittest.mock import MagicMock

from flowkit.llms.chat import Chat


class TestChat(unittest.TestCase):
    def test_format_results(self):
        llm = Chat(model="test_model", api_key="test_key")
        model_outputs = MagicMock(usage=MagicMock(total_tokens=7))
        model_outputs.choices = [{"message": {"role": "assistant", "content": "hi"}}]
        model_outputs.__getitem__.side_effect = {"usage": model_outputs.usage}.__getitem__
        text_result, call_data = llm._format_results(model_outputs, 0)
        self.assertEqual(text_result, "hi")
        self.assertEqual(call_data["tokens"], 7)
