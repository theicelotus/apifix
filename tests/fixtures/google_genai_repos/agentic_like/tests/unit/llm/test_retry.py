from google.api_core.retry import Retry


def test_retry_object():
    assert Retry(initial=1.0).initial == 1.0
