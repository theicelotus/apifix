import openai

from flowkit.retry import call_with_retry


class Completion:
    def __init__(self, model="gpt-3.5-turbo-instruct", api_key=None):
        self.model = model
        openai.api_key = api_key

    def _format_results(self, model_outputs, retries):
        """
        Formats the results of the model.

        Args:
            model_outputs: Raw output after model generation.
            retries: How many retries the call needed.
        """
        text_result = model_outputs.choices[0]["text"]
        call_data = {"raw_outputs": model_outputs, "retries": retries}
        return text_result, call_data

    def generate(self, prompt):
        completion, retries = call_with_retry(
            func=openai.Completion.create, model=self.model, prompt=prompt
        )
        return self._format_results(completion, retries)
