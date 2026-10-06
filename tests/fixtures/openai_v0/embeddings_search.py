"""Semantic search over a handful of documents."""

import openai
from openai.embeddings_utils import cosine_similarity, get_embedding

DOCS = ["How to reset a password", "Billing and invoices", "Shipping times"]


def embed(texts: list[str]) -> list[list[float]]:
    result = openai.Embedding.create(input=texts, engine="text-embedding-ada-002")
    return [row["embedding"] for row in result["data"]]


def search(query: str) -> str:
    q = get_embedding(query, engine="text-embedding-ada-002")
    scores = [cosine_similarity(q, d) for d in embed(DOCS)]
    return DOCS[scores.index(max(scores))]
