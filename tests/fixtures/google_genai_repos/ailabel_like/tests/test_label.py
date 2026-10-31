from unittest.mock import MagicMock, patch

from ailabel.lib.consumer import label


@patch("ailabel.lib.consumer.get_gemini")
def test_label(get_gemini):
    get_gemini.return_value.generate_content.return_value = MagicMock(text="spam")
    assert label("buy now") == "spam"
