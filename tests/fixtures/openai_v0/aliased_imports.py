"""Uses aliases and from-imports, which a naive grep would miss."""

import openai as oai
from openai import ChatCompletion as CC
from openai.error import InvalidRequestError, RateLimitError, Timeout


def classify(text: str) -> str:
    try:
        result = CC.create(model="gpt-4", messages=[{"role": "user", "content": text}])
    except InvalidRequestError as exc:
        raise ValueError("prompt rejected") from exc
    except (RateLimitError, Timeout):
        return "unknown"
    return result["choices"][0]["message"]["content"]


def moderate(text: str) -> bool:
    flags = oai.Moderation.create(input=text)
    return flags["results"][0]["flagged"]
