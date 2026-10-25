"""Retry helpers: call `func(*args, **kwargs)` until it stops raising."""

import asyncio
import time


def call_with_retry(func, exceptions_to_retry, max_retries, *args, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except exceptions_to_retry:
            if attempt == max_retries:
                raise
            time.sleep(0.1)


async def async_call_with_retry(async_func, exceptions_to_retry, max_retries, *args, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return await async_func(*args, **kwargs)
        except exceptions_to_retry:
            if attempt == max_retries:
                raise
            await asyncio.sleep(0.1)
