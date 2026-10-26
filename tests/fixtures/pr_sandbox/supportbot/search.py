"""Pick the help article closest to a question, by embedding similarity."""

import math

import openai

DOCS = ["How to reset a password", "Billing and invoices", "Shipping times"]
MODEL = "text-embedding-ada-002"


def embed(texts: list[str]) -> list[list[float]]:
    result = openai.Embedding.create(input=texts, model=MODEL)
    return [row["embedding"] for row in result["data"]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def search(query: str) -> str:
    query_vector, *doc_vectors = embed([query, *DOCS])
    scores = [_cosine(query_vector, d) for d in doc_vectors]
    return DOCS[scores.index(max(scores))]
