"""A repo already on openai 1.x whose tests still mock 0.x: reported, and fix stops early."""

from __future__ import annotations

import ast
import json
import shutil
import textwrap
from pathlib import Path

import pytest

from apifix import cli
from apifix.engine.agent import AgentConfig, fix
from apifix.engine.already_migrated import detect_already_migrated
from apifix.engine.migrations import get_migration
from apifix.engine.migrations.openai_v1.python_checks import v1_api_uses
from apifix.engine.scanner import scan
from tests.test_agent import FakeLLM, JudgingSandbox, go

STALE = Path(__file__).parent / "fixtures" / "stale_tests"
MIGRATION = get_migration("openai-v1")
MESSAGE = (
    "already on openai 1.x: the 2 findings are all in test files, which look out of date with "
    "the code"
)


@pytest.fixture
def stale(tmp_path):
    repo = tmp_path / "stale"
    shutil.copytree(STALE, repo)
    return repo


def test_stale_tests_fixture_is_already_migrated():
    result = scan(STALE, "openai-v1")
    assert {f.file for f in result.findings} == {"tests/test_gen.py"}
    already = result.already_migrated
    assert already is not None
    assert already.message == MESSAGE
    assert already.declarations == (
        "Pipfile:2 openai = '==1.12.0'",
        "requirements.txt:1 openai==1.12.0",
    )
    assert already.new_api_uses == ("src/topics/gen.py:7 openai.OpenAI",)
    assert already.test_files == ("tests/test_gen.py",)


def test_one_finding_is_worded_in_the_singular(stale):
    test = stale / "tests" / "test_gen.py"
    test.write_text(test.read_text().replace('{"choices": [{"message"', '{"choices": [{"x"'))
    already = scan(stale, "openai-v1").already_migrated
    assert already.message == (
        "already on openai 1.x: the finding is in a test file, which looks out of date with the "
        "code"
    )


@pytest.mark.parametrize(
    "spec",
    [
        "openai==0.28.1",  # a dependency finding: not only test files
        "openai",  # unpinned: 0.x is allowed
        "openai>=0.27",
        "openai<2",
    ],
)
def test_a_declaration_allowing_0x_is_not_migrated(stale, spec):
    (stale / "requirements.txt").write_text(f"{spec}\n")
    assert scan(stale, "openai-v1").already_migrated is None


def test_every_declaration_must_require_1x(stale):
    (stale / "Pipfile").write_text('[packages]\nopenai = "*"\n')
    assert scan(stale, "openai-v1").already_migrated is None


def test_no_declaration_is_not_migrated(stale):
    (stale / "requirements.txt").write_text("python-dotenv==1.0.1\n")
    (stale / "Pipfile").unlink()
    assert scan(stale, "openai-v1").already_migrated is None


def test_unparsable_dependency_file_is_not_migrated(stale):
    (stale / "pyproject.toml").write_text("[project\n")  # its pins are unknown
    assert scan(stale, "openai-v1").already_migrated is None


def test_a_finding_in_non_test_code_is_not_migrated(stale):
    (stale / "src" / "legacy.py").write_text(
        "import openai\nopenai.Completion.create(prompt='hi')\n"
    )
    assert scan(stale, "openai-v1").already_migrated is None


def test_non_test_code_must_use_the_1x_api(stale):
    # pinned to 1.x, but nothing shows the code was migrated (it may reach openai indirectly)
    (stale / "src" / "topics" / "gen.py").write_text("def generate_topics(subject):\n    ...\n")
    assert scan(stale, "openai-v1").already_migrated is None


def test_1x_api_in_test_files_does_not_count(stale):
    (stale / "src" / "topics" / "gen.py").write_text("def generate_topics(subject):\n    ...\n")
    (stale / "tests" / "test_client.py").write_text("import openai\nclient = openai.OpenAI()\n")
    assert scan(stale, "openai-v1").already_migrated is None


def test_no_findings_is_not_reported_as_migrated(stale):
    shutil.rmtree(stale / "tests")
    result = scan(stale, "openai-v1")
    assert result.findings == [] and result.already_migrated is None


def test_single_file_scan_is_never_migrated():
    assert scan(STALE / "tests" / "test_gen.py", "openai-v1").already_migrated is None


def test_detect_with_given_findings(stale):
    findings = scan(stale, "openai-v1").findings
    assert detect_already_migrated(stale, MIGRATION, findings).message == MESSAGE
    assert detect_already_migrated(stale, MIGRATION, []) is None


def _uses(source: str) -> list[tuple[int, str]]:
    return v1_api_uses(ast.parse(textwrap.dedent(source)))


def test_v1_api_uses_resolves_aliases_and_imports():
    assert _uses(
        """\
        import openai as oa
        from openai import AsyncOpenAI, BadRequestError as BRE

        client = AsyncOpenAI()
        oa.chat.completions.create(model="m", messages=[])
        oa.embeddings.create(model="m", input="x")
        try:
            pass
        except BRE:
            pass
        """
    ) == [
        (2, "openai.AsyncOpenAI"),
        (2, "openai.BadRequestError"),
        (4, "openai.AsyncOpenAI"),
        (5, "openai.chat"),
        (6, "openai.embeddings"),
        (9, "openai.BadRequestError"),
    ]


def test_v1_api_uses_ignores_0x_and_shared_names():
    assert (
        _uses(
            """\
            import openai
            from openai.error import InvalidRequestError
            from openai import APIError, RateLimitError

            openai.ChatCompletion.create(model="m", messages=[])
            openai.api_key = "k"
            client.chat.completions.create()  # not resolved to openai (e.g. another SDK)
            """
        )
        == []
    )


# --- scan and fix output --------------------------------------------------------------------


def test_scan_text_and_json(capsys):
    assert cli.main(["scan", str(STALE), "--migration", "openai-v1"]) == 1  # findings remain
    out = capsys.readouterr().out.splitlines()
    assert out[:4] == [
        MESSAGE,
        "  Declared: Pipfile:2 openai = '==1.12.0', requirements.txt:1 openai==1.12.0",
        "  New API in non-test code: src/topics/gen.py:7 openai.OpenAI",
        "  Test files with findings: tests/test_gen.py",
    ]
    assert out[4].startswith("tests/test_gen.py:6:12: response_access [likely]")
    assert cli.main(["scan", str(STALE), "--migration", "openai-v1", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["already_migrated"]["message"] == MESSAGE


def test_fix_stops_before_any_llm_call_or_sandbox_run(stale):
    llm, sandbox = FakeLLM([]), JudgingSandbox()
    result = go(stale, llm, sandbox)
    assert result.outcome == "already_migrated"
    assert result.reason == MESSAGE
    assert llm.requests == [] and sandbox.runs == []
    assert result.diff == "" and result.attempts == 0 and result.verification is None
    report = result.report_markdown
    assert report.startswith("# apifix fix: Already migrated (only the tests look out of date)\n")
    assert "- New API in non-test code: src/topics/gen.py:7 openai.OpenAI" in report
    assert "No LLM was called and no sandbox was run" in report
    assert (
        "- `tests/test_gen.py:10` api_call [likely] "
        '`"src.topics.gen.openai.ChatCompletion.create"`: String reference' in report
    )
    job = result.trajectory["job"]
    assert list(job["already_migrated"]["test_files"]) == ["tests/test_gen.py"]
    assert result.trajectory["outcome"] == "already_migrated"


def test_fix_with_given_findings_also_stops(stale):
    llm = FakeLLM([])
    findings = scan(stale, "openai-v1").findings
    result = fix(
        stale,
        "openai-v1",
        llm=llm,
        sandbox=JudgingSandbox(),
        findings=findings,
        config=AgentConfig(),
    )
    assert result.outcome == "already_migrated" and llm.requests == []


def test_cli_fix_exit_code(stale, tmp_path, monkeypatch, capsys):
    llm = FakeLLM([])
    monkeypatch.setattr("apifix.engine.llm.get_llm", lambda *a, **k: llm)
    monkeypatch.setattr("apifix.engine.sandbox.Sandbox", lambda *a, **k: JudgingSandbox())
    out = tmp_path / "out"
    code = cli.main(["fix", str(stale), "--migration", "openai-v1", "--out", str(out)])
    assert code == cli.EXIT_ALREADY_MIGRATED == 3
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"already_migrated: {MESSAGE}"
    assert lines[1].startswith("  tests/test_gen.py:6:12: response_access [likely] {")
    assert lines[2] == (
        '  tests/test_gen.py:10:12: api_call [likely] "src.topics.gen.openai.ChatCompletion.create"'
    )
    assert lines[3].startswith("0 attempt(s), 0 tokens, $0.00")
    assert json.loads((out / "trajectory.json").read_text())["outcome"] == "already_migrated"
    assert llm.requests == []
