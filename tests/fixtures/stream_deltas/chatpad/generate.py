from typing import Any, Dict, Iterator, List

import openai

SYSTEM_PROMPT = "You are a helpful notebook assistant."

Completion = Dict[str, List[Dict[str, Any]]]


def content(completion: Completion) -> str:
    return completion["choices"][0]["message"]["content"]


def deltas(completion: Iterator[Completion]) -> Iterator[str]:
    for chunk in completion:
        delta = chunk["choices"][0]["delta"]
        if "content" in delta:
            yield delta["content"]


def generate_reply(history: List[Dict[str, str]], text: str, stream: bool = False) -> Iterator[str]:
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": text},
        ],
        stream=stream,
    )
    if stream:
        yield from deltas(response)
    else:
        yield content(response)


def summarize(text: str) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": f"Summarize: {text}"}],
    )
    return content(response)
