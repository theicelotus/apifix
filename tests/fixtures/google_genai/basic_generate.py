import os

import google.generativeai as genai

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel(
    "gemini-1.5-flash",
    system_instruction="be terse",
    generation_config=genai.GenerationConfig(temperature=0.2, max_output_tokens=256),
)


def ask(prompt: str) -> str | None:
    try:
        resp = model.generate_content(prompt)
        return resp.text
    except ValueError:
        return None
