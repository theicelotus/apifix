"""Ask a chat model to rate a passage (openai 0.x API)."""

import openai
import yaml
from dotenv import load_dotenv

load_dotenv()


def load_prompts(path):
    with open(path) as f:
        return yaml.safe_load(f)


def rate(passage, model="gpt-4"):
    response = openai.ChatCompletion.create(
        model=model, messages=[{"role": "user", "content": f"Rate 1-9: {passage}"}]
    )
    return int(response["choices"][0]["message"]["content"].strip())
