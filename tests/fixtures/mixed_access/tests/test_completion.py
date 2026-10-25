import unittest
from unittest.mock import MagicMock, patch

from flowkit.llms.completion import Completion


class TestCompletion(unittest.TestCase):
    def setUp(self):
        self.llm = Completion(model="test_model", api_key="test_key")

    @patch("openai.Completion.create")
    def test_generate(self, mock_create):
        mock_output = MagicMock()
        mock_output.choices = [{"text": "test_text"}]
        mock_create.return_value = mock_output
        text_result, call_data = self.llm.generate("Hello")
        self.assertEqual(text_result, "test_text")
        self.assertEqual(call_data["raw_outputs"], mock_output)

    def test_format_results(self):
        # Mock the model_outputs and retries arguments
        model_outputs = MagicMock()
        model_outputs.choices = [{"text": "test_text"}]
        retries = 0

        text_result, call_data = self.llm._format_results(model_outputs, retries)

        self.assertEqual(text_result, "test_text")
        self.assertEqual(call_data["retries"], 0)
