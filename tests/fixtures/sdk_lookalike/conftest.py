from unittest.mock import MagicMock, patch

import pytest

canned_reply = "The lighthouse went dark."


@pytest.fixture
def reply():
    return canned_reply


@pytest.fixture(scope="session")
def mock_openai():
    with patch("openai.ChatCompletion.create") as mock_create:
        mock_create.return_value = {"choices": [{"message": {"content": canned_reply}}]}
        yield mock_create


@pytest.fixture(scope="session")
def mock_acme():
    canned_reply = MagicMock()
    canned_reply.choices = [MagicMock(message=MagicMock(content=canned_reply))]

    acme_client = MagicMock()
    acme_client.chat.completions.create.return_value = canned_reply

    with patch("acmechat.AcmeChat", return_value=acme_client):
        yield acme_client
