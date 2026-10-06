"""A small support chatbot written against openai 0.28."""

import logging
import os
import time

import openai

openai.api_key = os.getenv("OPENAI_API_KEY")
log = logging.getLogger(__name__)

SYSTEM_PROMPT = "You are a helpful support agent for Acme Corp."


def answer(question: str, history: list[dict]) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
    messages.append({"role": "user", "content": question})
    for attempt in range(3):
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.2,
                request_timeout=30,
            )
            log.info("used %s tokens", resp["usage"]["total_tokens"])
            return resp["choices"][0]["message"]["content"].strip()
        except openai.error.RateLimitError:
            time.sleep(2**attempt)
    raise RuntimeError("rate limited")
