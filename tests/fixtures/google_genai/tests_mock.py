from unittest import mock

import app.llm as llm


def test_ask(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    with mock.patch("app.llm.genai.GenerativeModel") as model_cls:
        model_cls.return_value.generate_content.return_value.text = "hello"
        assert llm.ask("hi") == "hello"


def test_configure(monkeypatch):
    with mock.patch("app.llm.genai.configure") as configure:
        llm.setup("key")
        configure.assert_called_once_with(api_key="key")
