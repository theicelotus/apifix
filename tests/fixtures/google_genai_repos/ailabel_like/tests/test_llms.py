from unittest import mock
from unittest.mock import patch

from ailabel.lib import llms


@patch("ailabel.lib.llms.genai.GenerativeModel")
@patch("ailabel.lib.llms.genai.configure")
def test_get_gemini(configure, model):
    assert llms.get_gemini() is model.return_value


def test_old_module_target():
    with patch("google.generativeai.GenerativeModel.generate_content") as call:
        call.return_value.text = "x"
        assert llms.ask("hi") == "x"


def test_patch_object(monkeypatch):
    with patch.object(llms.genai, "GenerativeModel") as model:
        assert llms.get_gemini() is model.return_value
    monkeypatch.setattr("ailabel.lib.llms.genai.configure", lambda **kw: None)


def test_client_target_is_fine():
    with mock.patch("ailabel.lib.llms.genai.Client"):
        pass
    with mock.patch("ailabel.lib.llms.os.environ"):
        pass
