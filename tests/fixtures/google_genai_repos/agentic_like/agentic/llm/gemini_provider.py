import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from google.api_core.exceptions import ServiceUnavailable


class GeminiProvider:
    def __init__(self, api_key):
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel("gemini-1.5-pro")

    def complete(self, prompt):
        try:
            return self.model.generate_content(prompt).text
        except google_exceptions.ResourceExhausted:
            raise
        except (ServiceUnavailable, google_exceptions.Aborted):
            return ""
