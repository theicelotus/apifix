def call_with_retry(func, max_retries=3, **kwargs):
    """Call func(**kwargs), retrying on any error; returns (result, retries)."""
    for retries in range(max_retries + 1):
        try:
            return func(**kwargs), retries
        except Exception:
            if retries == max_retries:
                raise
    raise RuntimeError("unreachable")
