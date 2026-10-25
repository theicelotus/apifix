"""Embeddings through a retry wrapper (0.x API)."""

import openai


def call_with_retry(func, max_retries=3, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return func(**kwargs)
        except openai.error.RateLimitError:
            if attempt == max_retries:
                raise


class Embedder:
    def __init__(self, model, deployment=None):
        self.model = model
        self.deployment = deployment

    def embed(self, texts):
        result = call_with_retry(
            func=openai.Embedding.create,
            engine=self.model,
            input=texts,
        )
        return [d["embedding"] for d in result["data"]]

    def embed_azure(self, texts):
        result = call_with_retry(
            func=openai.Embedding.create,
            deployment_id=self.deployment,
            input=texts,
        )
        return [d["embedding"] for d in result["data"]]
