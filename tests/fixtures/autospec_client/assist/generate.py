"""Suggestions for a notebook cell's error."""

import openai


def suggest(error: str) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": f"Explain this error: {error}"}],
    )
    return response["choices"][0]["message"]["content"]
