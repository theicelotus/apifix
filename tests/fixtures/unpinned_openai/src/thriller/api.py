"""Rate the suspense of a passage with a chat model (openai 0.x API)."""

import openai


def rate(passage, model="gpt-4"):
    response = openai.ChatCompletion.create(
        model=model,
        messages=[{"role": "user", "content": f"Rate the suspense (1-9): {passage}"}],
    )
    return int(response["choices"][0]["message"]["content"].strip())
