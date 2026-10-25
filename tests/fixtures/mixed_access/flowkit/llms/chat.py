import openai

from flowkit.retry import call_with_retry


class Chat:
    def __init__(self, model="gpt-3.5-turbo", api_key=None):
        self.model = model
        openai.api_key = api_key

    def _format_results(self, model_outputs, retries):
        text_result = model_outputs.choices[0]["message"]["content"]
        tokens = model_outputs["usage"].total_tokens
        return text_result, {"retries": retries, "tokens": tokens}

    def generate(self, messages):
        completion, retries = call_with_retry(
            func=openai.ChatCompletion.create, model=self.model, messages=messages
        )
        return self._format_results(completion, retries)
