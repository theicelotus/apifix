"""Looks a bit like openai code but has nothing to do with it: zero findings."""

import json


class ChatCompletion:
    @staticmethod
    def create(**kwargs):
        return {"choices": [{"message": {"content": "stub"}}]}


def first_choice(payload: str) -> str:
    resp = json.loads(payload)
    return resp["choices"][0]["message"]["content"]


def local() -> str:
    resp = ChatCompletion.create(engine="x")
    return resp["choices"][0]["message"]["content"]
