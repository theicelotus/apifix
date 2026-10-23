"""Rates how tense a passage reads, from 1 to 5."""

import openai

from src.storyteller.llm import generate


def rate(passage, settings):
    prompt = [{"role": "user", "content": f"Rate the tension of this passage: {passage}"}]
    if settings.get("provider", "openai") == "openai":
        result = openai.ChatCompletion.create(model=settings["model"], messages=prompt)
        text = result["choices"][0]["message"]["content"]
    else:
        text = generate(prompt, settings)
    return int(text.strip())
