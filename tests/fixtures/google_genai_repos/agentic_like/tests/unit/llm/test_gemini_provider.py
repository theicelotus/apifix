from unittest.mock import patch

import pytest
from google.api_core import exceptions as google_exceptions

from agentic.llm.gemini_provider import GeminiProvider


def test_quota_error_is_raised():
    with patch("agentic.llm.gemini_provider.genai"):
        provider = GeminiProvider("key")
        provider.model.generate_content.side_effect = google_exceptions.ResourceExhausted("quota")
        with pytest.raises(google_exceptions.ResourceExhausted):
            provider.complete("hi")
