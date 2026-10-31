"""String patch targets and google.api_core findings (google-genai), on repo-shaped fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

from apifix.engine.migrations import get_migration
from apifix.engine.scanner import scan, scan_text
from tests.conftest import at

REPOS = Path(__file__).parent / "fixtures" / "google_genai_repos"


def _findings(repo: Path, name: str):
    result = scan(repo, "google-genai")
    assert result.parse_errors == []
    return [f for f in result.findings if f.file == name]


# --- 4. string patch targets ------------------------------------------------------------------


def test_string_patch_targets_naming_removed_attributes_are_findings_in_tests():
    findings = _findings(REPOS / "ailabel_like", "tests/test_llms.py")
    model = at(findings, 7)  # @patch("ailabel.lib.llms.genai.GenerativeModel")
    assert model.old_pattern == '"ailabel.lib.llms.genai.GenerativeModel"'
    assert "ailabel.lib.llms.genai.Client" in model.suggestion
    assert model.category == "api_call" and model.confidence == "likely"
    configure = at(findings, 8)
    assert configure.category == "client_config"
    assert "ailabel.lib.llms.genai.Client" in configure.suggestion
    old_module = at(findings, 14)  # patch("google.generativeai.GenerativeModel.generate_content")
    assert "google.genai.Client" in old_module.suggestion


def test_patch_object_and_monkeypatch_targets_are_findings():
    findings = _findings(REPOS / "ailabel_like", "tests/test_llms.py")
    obj = at(findings, 20)  # patch.object(llms.genai, "GenerativeModel")
    assert "llms.genai.GenerativeModel" in obj.suggestion
    assert "llms.genai.Client" in obj.suggestion
    setattr_ = at(findings, 22)  # monkeypatch.setattr("ailabel.lib.llms.genai.configure", ...)
    assert setattr_.category == "client_config"


def test_targets_the_new_sdk_has_are_not_findings():
    findings = _findings(REPOS / "ailabel_like", "tests/test_llms.py")
    assert [f.line for f in findings if f.line >= 25] == []  # genai.Client, os.environ


def test_string_patch_targets_are_only_findings_in_test_files(tmp_path):
    (tmp_path / "app.py").write_text('def f(patch):\n    patch("pkg.genai.GenerativeModel")\n')
    assert scan(tmp_path, "google-genai").findings == []


# --- 5. google.api_core -----------------------------------------------------------------------


def test_api_core_in_source_and_tests_is_a_certain_finding_when_undeclared():
    repo = REPOS / "agentic_like"
    source = _findings(repo, "agentic/llm/gemini_provider.py")
    cats = {(f.line, f.category) for f in source}
    assert (2, "import") in cats and (3, "import") in cats
    assert (14, "error_class") in cats  # google_exceptions.ResourceExhausted
    assert at(source, 3, "import").confidence == "certain"
    line16 = {f.old_pattern: f for f in source if f.line == 16}  # one finding per exception
    assert set(line16) == {"ServiceUnavailable", "google_exceptions.Aborted"}
    assert "ServerError" in line16["ServiceUnavailable"].suggestion
    assert "APIError" in line16["google_exceptions.Aborted"].suggestion  # no table entry

    tests = _findings(repo, "tests/unit/llm/test_gemini_provider.py")
    assert {(f.line, f.category) for f in tests} >= {
        (4, "import"),
        (12, "error_class"),
        (13, "error_class"),
    }
    assert all(f.confidence == "certain" for f in tests)
    exhausted = at(tests, 12, "error_class")
    assert (
        "google.genai.errors.ClientError" in exhausted.suggestion and "429" in exhausted.suggestion
    )
    assert "google-api-core is no longer installed" in exhausted.suggestion

    # an import alone (google.api_core.retry) is a finding too: nothing provides the module
    retry = _findings(repo, "tests/unit/llm/test_retry.py")
    assert [(f.line, f.category, f.confidence) for f in retry] == [(1, "import", "certain")]


def test_bare_imported_exception_names_are_findings(tmp_path):
    repo = _copy(tmp_path)
    source = _findings(repo, "agentic/llm/gemini_provider.py")
    names = {f.old_pattern for f in source if f.category == "error_class"}
    assert "ServiceUnavailable" in names


def _copy(tmp_path: Path, name: str = "agentic_like") -> Path:
    repo = tmp_path / name
    shutil.copytree(REPOS / name, repo)
    return repo


def test_declared_google_api_core_keeps_the_import_and_the_exception_findings_stay_likely(
    tmp_path,
):
    repo = _copy(tmp_path)
    (repo / "requirements.txt").write_text("google-generativeai>=0.8\ngoogle-api-core\npytest\n")
    result = scan(repo, "google-genai").findings
    core = [f for f in result if "google-api-core is no longer installed" in f.suggestion]
    assert core and all(f.category == "error_class" and f.confidence == "likely" for f in core)
    assert _findings(repo, "tests/unit/llm/test_retry.py") == []


def test_a_dependency_of_api_core_counts_as_declaring_it(tmp_path):
    repo = _copy(tmp_path)
    (repo / "requirements.txt").write_text("google-generativeai>=0.8\ngoogle-cloud-storage\n")
    assert not [
        f
        for f in scan(repo, "google-genai").findings
        if f.category == "import" and "api_core" in f.old_pattern
    ]


def test_api_core_alone_is_not_a_gemini_repo(tmp_path):
    (tmp_path / "requirements.txt").write_text("google-cloud-storage\n")
    (tmp_path / "store.py").write_text(
        "from google.api_core import exceptions\n\n\n"
        "def f(c):\n    try:\n        c.get()\n    except exceptions.NotFound:\n        pass\n"
    )
    assert scan(tmp_path, "google-genai").findings == []


def test_a_rewrite_that_keeps_the_import_still_has_the_finding(tmp_path):
    """What the completeness check sees: scan_text on the candidate, with the repo's root."""
    repo = _copy(tmp_path)
    path = "tests/unit/llm/test_retry.py"
    others = [f for f in scan(repo, "google-genai").findings if f.file != path]
    kept = (repo / path).read_text()
    found = scan_text(path, kept, get_migration("google-genai"), others=others, root=repo)
    assert [(f.category, f.confidence) for f in found] == [("import", "certain")]
    moved = "from google.genai import errors\n\n\ndef test_x():\n    assert errors.APIError\n"
    assert scan_text(path, moved, get_migration("google-genai"), others=others, root=repo) == []
