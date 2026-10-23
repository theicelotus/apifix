"""Continues a story with one of two chat providers."""

import openai
from acmechat import AcmeChat


def generate(messages, settings):
    """The model's reply to `messages`, from the provider `settings` names."""
    provider = settings.get("provider", "openai")

    if provider == "openai":
        response = openai.ChatCompletion.create(
            model=settings["model"],
            messages=messages,
            max_tokens=settings["max_tokens"],
        )

        return response["choices"][0]["message"]["content"]

    elif provider == "acme":
        client = AcmeChat(api_key=settings["api_key"])
        reply = client.chat.completions.create(
            model=settings["model"],
            messages=messages,
        )
        return reply.choices[0].message.content

    raise ValueError(f"unknown provider: {provider}")
