"""Summaries of many notebooks at once (a command-line tool; not used by the magics)."""

import openai


def summarize(texts):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Summarize:\n" + "\n".join(texts)}],
    )
    return response["choices"][0]["message"]["content"]
