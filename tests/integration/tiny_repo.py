"""A tiny openai 0.28 repo with mocked tests, shared by the Docker and live LLM tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

ORIGINAL = {
    "requirements.txt": "openai==0.28.1\npytest\n",
    "app.py": """\
        import openai


        def ask(question):
            response = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": question}],
            )
            return response["choices"][0]["message"]["content"].strip()
        """,
    "tests/test_app.py": """\
        from unittest import mock

        from app import ask


        def completion(text):
            return {"choices": [{"message": {"content": text}}]}


        @mock.patch("openai.ChatCompletion.create")
        def test_ask(create):
            create.return_value = completion("hello")
            assert ask("hi") == "hello"


        @mock.patch("openai.ChatCompletion.create")
        def test_strips_whitespace(create):
            create.return_value = completion("  hello\\n")
            assert ask("hi") == "hello"
        """,
}


def make_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    return root
