import google.generativeai as genai
from google.api_core import exceptions as google_exceptions


class GeminiProvider:
    def __init__(self, api_key: str) -> None:
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(
            "gemini-1.5-pro",
            safety_settings=[
                {"category": "harassment", "threshold": "block_none"},
            ],
        )

    def complete(self, prompt: str) -> str:
        try:
            resp = self.model.generate_content(prompt)
        except google_exceptions.ResourceExhausted:
            raise
        except google_exceptions.DeadlineExceeded:
            raise
        used = resp.usage_metadata.total_token_count
        return resp.text or ""
