"""Streams a completion to the terminal using the async 0.x API."""

import asyncio

import openai


async def stream_reply(prompt: str) -> str:
    parts = []
    response = await openai.ChatCompletion.acreate(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    )
    async for chunk in response:
        delta = chunk["choices"][0]["delta"]
        text = delta.get("content", "")
        print(text, end="", flush=True)
        parts.append(text)
    return "".join(parts)


if __name__ == "__main__":
    asyncio.run(stream_reply("Write a haiku about migrations."))
