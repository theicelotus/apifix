"""Half-migrated 1.x code: the clients are right, some 0.x per-call kwargs are left."""

import openai
from openai import AzureOpenAI, OpenAI, RateLimitError

from wrapkit.retry import call_with_retry


def complete(key, endpoint, deployment, prompt):
    client = AzureOpenAI(azure_endpoint=endpoint, api_version="2024-02-01", api_key=key)
    return call_with_retry(
        func=client.completions.create,
        exceptions_to_retry=(RateLimitError,),
        max_retries=3,
        engine=deployment,
        prompt=prompt,
    )


def embed(texts):
    return openai.embeddings.create(engine="text-embedding-3-small", input=texts)


def run(func, api_key, **kwargs):
    """Calls `func` with the key in a header; `api_key` is this wrapper's own."""
    return func(extra_headers={"api-key": api_key}, **kwargs)


class Bot:
    def __init__(self):
        self.client = OpenAI()

    def ask(self, question):
        return self.client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": question}], request_id="q1"
        )

    def ask_slowly(self, question):
        slow = self.client.with_options(timeout=60)
        return slow.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": question}], headers={}
        )

    def ask_through_run(self, key, question):
        return run(
            self.client.chat.completions.create,
            api_key=key,
            api_version="2024-02-01",
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
        )

    def ask_correctly(self, question):
        return self.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
            timeout=30,
            extra_headers={"x-request-id": "q1"},
        )


def unknown_client(client, question):
    # `client` is a parameter: other SDKs share these method names, so it is not checked
    return client.chat.completions.create(engine="x", messages=[question])
