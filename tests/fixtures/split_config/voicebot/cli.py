"""Command-line entry point for the voice bot."""

import os

import openai

from voicebot.assistant import Assistant


def main(api_key: str = os.getenv("VOICEBOT_OPENAI_KEY"), base_url: str | None = None) -> None:
    """Configure the library, then answer one question from stdin."""
    openai.api_key = api_key
    openai.api_base = base_url
    print(Assistant().reply(input("> ")))


if __name__ == "__main__":
    main()
