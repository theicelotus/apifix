import google.generativeai as genai

genai.configure(api_key="dummy")


def embed_one(text: str) -> list[float]:
    result = genai.embed_content(model="models/embedding-001", content=text, task_type="RETRIEVAL_DOCUMENT")
    return result["embedding"]


def embed_many(texts: list[str]) -> list[list[float]]:
    result = genai.embed_content(model="models/embedding-001", content=texts)
    return [e["embedding"] for e in result]
