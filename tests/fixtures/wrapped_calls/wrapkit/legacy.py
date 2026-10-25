"""0.x code: the API is called through retry wrappers."""

import functools

import openai
from openai.error import RateLimitError

from wrapkit.retry import async_call_with_retry, call_with_retry


class AzureCompleter:
    def __init__(self, key, endpoint, deployment):
        openai.api_type = "azure"
        openai.api_key = key
        openai.api_base = endpoint
        self._deployment = deployment

    def complete(self, prompt):
        return call_with_retry(
            func=openai.Completion.create,
            exceptions_to_retry=(RateLimitError,),
            max_retries=3,
            engine=self._deployment,
            model="gpt-35-turbo",
            prompt=prompt,
        )

    async def acomplete(self, prompt):
        return await async_call_with_retry(
            async_func=openai.Completion.acreate,
            exceptions_to_retry=(RateLimitError,),
            max_retries=3,
            deployment_id=self._deployment,
            prompt=prompt,
        )


def embed(texts):
    return call_with_retry(
        openai.Embedding.create, (RateLimitError,), 3, engine="text-embedding-ada-002", input=texts
    )


chat = functools.partial(openai.ChatCompletion.create, request_timeout=30)
