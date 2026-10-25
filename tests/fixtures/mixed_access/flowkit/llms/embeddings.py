import openai

from flowkit.retry import call_with_retry


class Embeddings:
    def __init__(self, model="text-embedding-ada-002", api_key=None):
        self.model = model
        openai.api_key = api_key

    def embed(self, texts):
        result, _ = call_with_retry(func=openai.Embedding.create, model=self.model, input=texts)
        return [result["data"][i]["embedding"] for i in range(len(texts))]

    def first(self, result):
        return result.data[0]["embedding"]

    def from_json(self, payload):
        import json

        data = json.loads(payload)
        return data["data"][0]["embedding"]
