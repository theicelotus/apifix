"""Dependency and pin-edit tests for the google-generativeai -> google-genai migration."""

from __future__ import annotations

import pytest

from apifix.engine.migrations import get_migration
from apifix.engine.pin_edits import plan_pin_edits
from apifix.engine.scanner import scan

MIGRATION = get_migration("google-genai")


def test_source_package_is_flagged_at_any_version(tmp_path):
    (tmp_path / "requirements.txt").write_text("google-generativeai==0.8.4\n")
    (finding,) = scan(tmp_path, "google-genai").findings
    assert finding.category == "dependency" and finding.confidence == "certain"
    assert "no longer supported" in finding.suggestion
    assert "google-genai>=1.19.0,<3" in finding.suggestion


@pytest.mark.parametrize(
    ("line", "confidence", "says"),
    [
        ("google-genai", "likely", "the declaration has no version"),
        ("google-genai>=0.3", "likely", "still allows an older release"),
        ("google-genai<2", "likely", "still allows an older release"),
        ("google-genai==0.8.4", "certain", "Pin google-genai"),
    ],
)
def test_loose_or_old_google_genai_declarations_are_flagged(tmp_path, line, confidence, says):
    (tmp_path / "requirements.txt").write_text(f"{line}\n")
    (finding,) = scan(tmp_path, "google-genai").findings
    assert (finding.category, finding.confidence) == ("dependency", confidence)
    assert says in finding.suggestion and "google-genai>=1.19.0,<3" in finding.suggestion


@pytest.mark.parametrize(
    "line",
    [
        "google-genai>=1.0",
        "google-genai>=1.19.0,<3",
        "google-genai==2.34.0",
        "google-genai @ git+https://x/g",
    ],
)
def test_google_genai_declarations_on_new_api_or_direct_are_not_flagged(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(f"{line}\n")
    assert scan(tmp_path, "google-genai").findings == []


def test_pin_edit_renames_the_distribution(tmp_path):
    (tmp_path / "requirements.txt").write_text("google-generativeai==0.8.4\n")
    result = scan(tmp_path, "google-genai")
    plan = plan_pin_edits(tmp_path, result.findings, MIGRATION)
    (f,) = plan.files
    (e,) = [e for e in f.edits if e.kind == "migration"]
    assert e.old == "google-generativeai==0.8.4" and e.new == "google-genai>=1.19.0,<3"


def test_pin_edit_removes_old_entry_when_google_genai_is_already_declared(tmp_path):
    (tmp_path / "requirements.txt").write_text("google-generativeai==0.8.4\ngoogle-genai>=1.0\n")
    result = scan(tmp_path, "google-genai")
    plan = plan_pin_edits(tmp_path, result.findings, MIGRATION)
    (f,) = plan.files
    assert "google-generativeai" not in f.text
    assert "google-genai>=1.19.0,<3" in f.text  # the existing declaration raised to the floor too
