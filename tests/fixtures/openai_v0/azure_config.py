"""Azure OpenAI configuration, 0.x style."""

import os

import openai

openai.api_type = "azure"
openai.api_base = os.environ["AZURE_OPENAI_ENDPOINT"]
openai.api_version = "2023-05-15"
openai.api_key = os.environ["AZURE_OPENAI_KEY"]


def summarize(text: str) -> str:
    completion = openai.ChatCompletion.create(
        deployment_id="gpt-35-turbo-prod",
        messages=[{"role": "user", "content": f"Summarize:\n{text}"}],
    )
    return completion.choices[0].message.content
