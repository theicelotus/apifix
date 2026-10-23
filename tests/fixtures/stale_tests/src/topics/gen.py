import os

import openai


def generate_topics(subject: str) -> list[str]:
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Five essay topics about {subject}"}],
    )
    return [line for line in (reply.choices[0].message.content or "").splitlines() if line]
