import unittest
from unittest.mock import patch

from embedkit.embeddings import Embedder


class TestEmbedder(unittest.TestCase):
    def setUp(self):
        self.embedder = Embedder(model="test_model", deployment="test_deployment")

    @patch("openai.Embedding.create", autospec=True)
    def test_embed(self, mock_create):
        # Define the mock return value
        mock_create.return_value = {"data": [{"embedding": [0.1]}]}

        # Call the method under test
        result = self.embedder.embed(["doc"])

        # Check the request
        mock_create.assert_called_once_with(
            engine="test_model",
            input=["doc"],
        )
        self.assertEqual(result, [[0.1]])

    @patch("openai.Embedding.create", autospec=True)
    def test_embed_kwargs(self, mock_create):
        # Define the mock return value
        mock_create.return_value = {"data": [{"embedding": [0.2]}]}

        # Call the method under test
        self.embedder.embed(["doc"])

        # Check the request
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["engine"], "test_model")

    @patch("openai.Embedding.create", autospec=True)
    def test_embed_azure(self, mock_create):
        # Define the mock return value
        mock_create.return_value = {"data": [{"embedding": [0.3]}]}

        # Call the method under test
        self.embedder.embed_azure(["doc"])

        # Check the request
        mock_create.assert_called_once_with(
            deployment_id="test_deployment",
            input=["doc"],
        )

    def test_embedder_settings(self):
        self.assertEqual(self.embedder.model, "test_model")
        self.assertEqual(self.embedder.deployment, "test_deployment")
