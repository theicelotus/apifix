"""Scanner tests for the google-generativeai -> google-genai migration."""

from __future__ import annotations

from pathlib import Path

from apifix.engine.scanner import scan
from tests.conftest import at, summary

FIXTURES = Path(__file__).parent / "fixtures" / "google_genai"


def _scan(name: str):
    result = scan(FIXTURES / name, "google-genai")
    assert result.parse_errors == []
    return result.findings


def test_basic_generate():
    findings = _scan("basic_generate.py")
    assert summary(findings) == [
        (3, "import"),
        (5, "client_config"),
        (7, "api_call"),
        (10, "api_call"),
        (16, "api_call"),
        (17, "response_access"),
    ]
    assert at(findings, 3).suggestion == "from google import genai"
    client_config = at(findings, 5)
    assert client_config.old_pattern == 'genai.configure(api_key=os.getenv("GEMINI_API_KEY"))'
    assert "genai.Client" in client_config.suggestion
    model = at(findings, 7)
    assert "GenerativeModel" in model.old_pattern and model.confidence == "certain"
    config = at(findings, 10)
    assert "GenerationConfig" in config.old_pattern
    call = at(findings, 16)
    assert "client.models.generate_content" in call.suggestion
    text = at(findings, 17)
    assert text.old_pattern == "resp.text" and "None" in text.suggestion


def test_provider_class_tracks_self_attribute_and_api_core_errors():
    findings = _scan("provider_class.py")
    cats = summary(findings)
    assert (8, "api_call") in cats  # self.model = genai.GenerativeModel(...)
    call = at(findings, 17, "api_call")
    assert call.old_pattern == "self.model.generate_content(prompt)"
    exhausted = at(findings, 18, "error_class")
    assert "ClientError" in exhausted.suggestion and "429" in exhausted.suggestion
    deadline = at(findings, 20, "error_class")
    assert "ServerError" in deadline.suggestion


def test_chat_stream_tracks_chat_instance_and_history():
    findings = _scan("chat_stream.py")
    start_chat = at(findings, 5, "api_call")
    assert "client.chats.create" in start_chat.suggestion
    send = at(findings, 10, "api_call")
    assert "send_message" in send.old_pattern and "it streams" in send.suggestion
    assert "send_message_stream" in send.suggestion
    history = at(findings, 16, "response_access")
    assert history.old_pattern == "chat.history"
    assert "get_history" in history.suggestion


def test_embeddings_dict_access():
    findings = _scan("embeddings.py")
    one = at(findings, 8, "response_access")
    assert one.old_pattern == 'result["embedding"]' and one.confidence == "certain"
    many = at(findings, 13, "response_access")
    assert many.confidence == "likely"  # not a name bound from embed_content itself


def test_aliases_bare_import_and_from_import():
    findings = _scan("aliases.py")
    assert summary(findings) == [
        (1, "import"),
        (3, "import"),
        (5, "client_config"),
        (6, "api_call"),
        (7, "api_call"),
    ]


def test_new_sdk_has_no_findings():
    assert _scan("new_sdk.py") == []


def test_out_of_scope_is_still_reported():
    findings = _scan("out_of_scope.py")
    upload = at(findings, 7, "api_call")
    assert "Files API out of scope" in upload.suggestion
    legacy = at(findings, 11, "api_call")
    assert "PaLM-era" in legacy.suggestion


def test_no_findings_for_names_from_other_google_packages(tmp_path):
    (tmp_path / "app.py").write_text(
        "from google.cloud import storage\nfrom google.protobuf import message\n"
        "client = storage.Client()\n"
    )
    assert scan(tmp_path, "google-genai").findings == []
