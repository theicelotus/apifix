"""Relays replies from another vendor's SDK, whose stream deltas look like openai's."""

from typing import Any, Iterator


def relay(stream: Any) -> Iterator[str]:
    for chunk in stream:
        delta = chunk.choices[0].delta
        if hasattr(delta, "content") and delta.content:
            yield delta.content
