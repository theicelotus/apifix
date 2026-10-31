from unittest.mock import patch

from ailabel.lib.llms import ask


def test_ask():
    with patch("ailabel.lib.llms.get_gemini") as get_gemini:
        get_gemini.return_value.generate_content.return_value.text = "ok"
        assert ask("hi") == "ok"
