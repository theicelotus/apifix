"""Replies from the chat model."""

import openai


def generate_reply(context, text):
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[*context, {"role": "user", "content": text}],
    )
    yield response["choices"][0]["message"]["content"]
