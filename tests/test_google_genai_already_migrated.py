"""`already_migrated` detection for the google-generativeai -> google-genai migration."""

from __future__ import annotations

from apifix.engine.already_migrated import detect_already_migrated
from apifix.engine.migrations import get_migration
from apifix.engine.scanner import scan

MIGRATION = get_migration("google-genai")

APP = (
    "from google import genai\n\n"
    'client = genai.Client(api_key="x")\n\n\n'
    "def ask(prompt):\n"
    "    return client.models.generate_content(model='gemini-1.5-flash', contents=prompt).text\n"
)
STALE_TEST = (
    "import google.generativeai as genai\n"
    "from unittest import mock\n\n\n"
    "def test_ask():\n"
    '    with mock.patch("app.client"):\n'
    '        genai.configure(api_key="test")\n'
)


def test_stale_test_only_repo_is_detected_as_already_migrated(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    (tmp_path / "test_app.py").write_text(STALE_TEST)
    (tmp_path / "requirements.txt").write_text("google-genai>=1.19.0,<3\n")
    result = scan(tmp_path, MIGRATION)
    assert result.already_migrated is not None
    assert "already on google-genai" in result.already_migrated.message
    assert any("genai.Client" in u for u in result.already_migrated.new_api_uses)


def test_not_already_migrated_when_the_old_distribution_is_still_declared(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    (tmp_path / "test_app.py").write_text(STALE_TEST)
    (tmp_path / "requirements.txt").write_text(
        "google-genai>=1.19.0,<3\ngoogle-generativeai==0.8.4\n"
    )
    result = scan(tmp_path, MIGRATION)
    assert result.already_migrated is None


def test_not_already_migrated_when_findings_are_outside_test_files(tmp_path):
    (tmp_path / "app.py").write_text("import google.generativeai as genai\n")
    (tmp_path / "requirements.txt").write_text("google-genai>=1.19.0,<3\n")
    result = scan(tmp_path, MIGRATION)
    assert result.already_migrated is None


def test_detect_already_migrated_directly(tmp_path):
    (tmp_path / "app.py").write_text(APP)
    (tmp_path / "test_app.py").write_text(STALE_TEST)
    (tmp_path / "requirements.txt").write_text("google-genai>=1.19.0,<3\n")
    result = scan(tmp_path, MIGRATION)
    already = detect_already_migrated(tmp_path, MIGRATION, result.findings)
    assert already is not None
    assert already.test_files == ("test_app.py",)
