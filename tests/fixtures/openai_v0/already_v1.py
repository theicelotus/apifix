"""Already migrated to openai 1.x: must produce zero findings."""

import os

import openai
from openai import OpenAI, RateLimitError

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def answer(question: str) -> str:
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
            timeout=30,
        )
    except (RateLimitError, openai.BadRequestError, openai.APITimeoutError):
        return ""
    return resp.choices[0].message.content or ""
