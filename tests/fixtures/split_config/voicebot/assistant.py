"""Answers spoken questions with a chat model."""

import openai

# Answers longer than this are cut off by the speech engine.
MAX_WORDS = 80

SYSTEM_PROMPT = """You are a friendly voice assistant.   
Keep every answer short, because it is read aloud.   
Then rate how  well the answer fits the question, from 1 to 5."""


def _clean(text: str) -> str:
    """Collapse whitespace so the speech engine reads it smoothly."""
    return " ".join(text.split()[:MAX_WORDS])


def first_choice(data):
    """The first choice of a stored completion."""
    return data["choices"][0]


class Assistant:
    """Wraps one chat model."""

    def __init__(self, voice: str = "calm") -> None:
        self.voice = voice

    def reply(self, question: str) -> str:
        # Ask the model, then tidy the answer for speech.
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
        )
        return _clean(response["choices"][0]["message"]["content"])

    def describe(self) -> str:
        # The voice is fixed per instance.
        return f"assistant with a {self.voice} voice"
