import unittest
from unittest.mock import patch

from flowkit.llms.embeddings import Embeddings


class TestEmbeddings(unittest.TestCase):
    @patch("openai.Embedding.create")
    def test_embed(self, mock_create):
        mock_create.return_value = {"data": [{"embedding": [0.1]}, {"embedding": [0.2]}]}
        self.assertEqual(Embeddings(api_key="k").embed(["a", "b"]), [[0.1], [0.2]])
