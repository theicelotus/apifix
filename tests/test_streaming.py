"""Streaming in openai 1.x: key checks on a stream delta (scanner finding, `streaming` rewrite
check), the stream= marker on call findings, and the per-file Streaming guide part. Fixture
stream_deltas is synthetic, modelled on a real repo's dict-typed `deltas()` helper."""

from __future__ import annotations

import ast
import shutil
import textwrap
from pathlib import Path

import pytest

from apifix.engine.migrations import get_migration
from apifix.engine.migrations.openai_v1 import rules
from apifix.engine.migrations.openai_v1.guide_parts import selected_parts
from apifix.engine.migrations.openai_v1.python_checks import check_python, stream_check_errors
from apifix.engine.migrations.openai_v1.repo_findings import adjust_findings
from apifix.engine.migrations.openai_v1.streaming import content_checks
from apifix.engine.scanner import scan
from apifix.engine.test_results import FileCoverage
from apifix.engine.whole_file import check_candidate
from tests.test_agent import FakeLLM, JudgingSandbox, block, wf
from tests.test_verifier import run

FIXTURE = Path(__file__).parent / "fixtures" / "stream_deltas"
MIGRATION = get_migration("openai-v1")
GENERATE = "chatpad/generate.py"
TESTS = "tests/test_generate.py"


def findings_of(source: str, file: str = "app.py"):
    source = textwrap.dedent(source)
    return check_python(file, ast.parse(source), source)


def checks_in(source: str) -> list[tuple[int, str, str]]:
    source = textwrap.dedent(source)
    return [(c.line, c.check, c.delta) for c in content_checks(ast.parse(source), source)]


# --- the detector -------------------------------------------------------------------------------


def test_delta_shapes_and_names():
    assert checks_in(
        """
        def a(stream):
            for chunk in stream:
                delta = chunk["choices"][0]["delta"]
                if "content" in delta:
                    yield delta["content"]

        def b(stream):
            for chunk in stream:
                if hasattr(chunk.choices[0].delta, "content"):
                    yield chunk.choices[0].delta.content

        def c(stream):
            for chunk in stream:
                for choice in chunk.choices:
                    if "content" not in choice.delta:
                        continue

        def d(stream):
            for chunk in stream:
                first = chunk["choices"][0]
                if hasattr(first["delta"], "content"):
                    pass

        def e(chunk):
            return [x for x in [chunk.choices[0].delta] if hasattr(x, "content")]
        """
    ) == [
        (5, '"content" in delta', "delta"),
        (10, 'hasattr(chunk.choices[0].delta, "content")', "chunk.choices[0].delta"),
        (16, '"content" not in choice.delta', "choice.delta"),
        (22, 'hasattr(first["delta"], "content")', 'first["delta"]'),
    ]


def test_other_key_checks_and_scopes_are_not_deltas():
    assert (
        checks_in(
            """
            def a(message, delta):
                if "content" in message:  # not a delta
                    pass
                if hasattr(delta, "content"):  # a parameter, not bound from a chunk here
                    pass
                if hasattr(delta, "role"):  # another key
                    pass

            def b(chunk):
                delta = chunk.choices[0].delta

            def c(delta):
                return "content" in delta  # b's delta is b's
            """
        )
        == []
    )


# --- scanner ------------------------------------------------------------------------------------


def test_fixture_findings():
    found = {(f.file, f.line): f for f in scan(FIXTURE, "openai-v1").findings}
    check = found[(GENERATE, 17)]
    assert (check.category, check.confidence, check.old_pattern) == (
        "response_access",
        "likely",
        '"content" in delta',
    )
    assert check.suggestion == rules.STREAM_CONTENT_CHECK.format(
        check='"content" in delta', delta="delta"
    )
    # stream=stream may stream; summarize() does not pass stream=
    assert found[(GENERATE, 22)].suggestion.endswith(rules.STREAM_CALL)
    assert not found[(GENERATE, 38)].suggestion.endswith(rules.STREAM_CALL)
    # the other SDK's stream: same key check, no openai import
    assert not [f for f in found.values() if f.file == "chatpad/relay.py"]


@pytest.mark.parametrize(
    ("kwarg", "streams"),
    [
        ("stream=True", True),
        ("stream=stream", True),
        ("stream=False", False),
        ("stream=None", False),
        ("temperature=0", False),
    ],
)
def test_the_stream_marker_on_call_findings(kwarg, streams):
    (f,) = findings_of(
        f"""
        import openai

        def ask(stream):
            return openai.ChatCompletion.create(model="m", messages=[], {kwarg})
        """
    )
    assert f.suggestion.endswith(rules.STREAM_CALL) is streams


def test_key_checks_need_an_openai_import():
    source = """
        def relay(stream):
            for chunk in stream:
                if hasattr(chunk.choices[0].delta, "content"):
                    yield chunk.choices[0].delta.content
        """
    assert findings_of(source) == []
    (f,) = findings_of("from openai import OpenAI\n" + textwrap.dedent(source))
    assert f.old_pattern == 'hasattr(chunk.choices[0].delta, "content")'


def test_module_client_suggestions_keep_the_stream_marker():
    findings = findings_of(
        """
        import openai

        openai.api_key = load_key()

        def ask(stream):
            return openai.ChatCompletion.create(model="m", messages=[], stream=True)
        """
    )
    call = next(f for f in adjust_findings(findings) if f.category == "api_call")
    assert call.suggestion.startswith("Use openai.chat.completions.create(...): the module-level")
    assert call.suggestion.endswith(rules.STREAM_CALL)


# --- guide --------------------------------------------------------------------------------------


def test_the_streaming_part_follows_files_that_stream():
    findings = scan(FIXTURE, "openai-v1").findings
    own = [f for f in findings if f.file == GENERATE]
    tests = [f for f in findings if f.file == TESTS]
    assert "streaming" in selected_parts(own)
    assert "streaming" not in selected_parts(tests)
    # the shared system prompt of a whole-file job leaves it to the files' own requests
    assert "streaming" not in selected_parts(findings, shared=True)
    text, parts = MIGRATION.job_guide(own)
    assert "streaming" in parts and "## Streaming" in text
    assert "if delta.content is not None:" in text
    _, parts = MIGRATION.job_guide(findings, shared=True)
    assert "streaming" not in parts
    assert MIGRATION.guide_text(["streaming"]).startswith("# openai 0.x -> 1.x\n\n## Streaming")
    # one call without stream= selects no streaming part
    (plain,) = findings_of("import openai\nopenai.ChatCompletion.create(model='m', messages=[])")
    assert "streaming" not in selected_parts([plain])


def test_examples_keep_the_assistant_role():
    assert 'role="assistant"' in MIGRATION.mock_responses["chat.completions.create"]
    tests = MIGRATION.guide.split("## Tests")[1].split("## Dependencies")[0]
    assert "SimpleNamespace(content=" not in tests


# --- the rewrite check --------------------------------------------------------------------------

ORIGINAL = (FIXTURE / GENERATE).read_text()


def migrated(check: str = "if delta.content is not None:") -> str:
    text = ORIGINAL.replace("import openai\n", "from openai import OpenAI\n")
    text = text.replace(
        'completion["choices"][0]["message"]["content"]', "completion.choices[0].message.content"
    )
    text = text.replace(
        '        delta = chunk["choices"][0]["delta"]\n'
        '        if "content" in delta:\n'
        '            yield delta["content"]\n',
        "        delta = chunk.choices[0].delta\n"
        f"        {check}\n"
        "            yield delta.content\n",
    )
    return text.replace(
        "    response = openai.ChatCompletion.create(",
        "    client = OpenAI()\n    response = client.chat.completions.create(",
    )


@pytest.mark.parametrize(
    "check",
    [
        'if hasattr(delta, "content"):',
        'if "content" in delta:',
        'if hasattr(delta, "content") and delta.content:',
    ],
)
def test_key_checks_in_a_rewrite_are_rejected(check):
    (error,) = stream_check_errors(GENERATE, ORIGINAL, migrated(check))
    shown = check.removeprefix("if ").removesuffix(":")
    shown = shown if "and" not in shown else 'hasattr(delta, "content")'
    assert error == (
        "line 17: in 1.x a stream delta's content always exists and is None on role and final "
        f"chunks, so `{shown}` must become `if delta.content is not None:` "
        '(or `delta.content or ""`)'
    )


def test_none_checks_pass_and_other_sdks_keep_theirs():
    assert stream_check_errors(GENERATE, ORIGINAL, migrated()) == []
    assert stream_check_errors(GENERATE, ORIGINAL, migrated("if delta.content:")) == []
    relay = (FIXTURE / "chatpad/relay.py").read_text()
    assert stream_check_errors("chatpad/relay.py", relay, relay + "\n# reviewed\n") == []
    # a new key check in that file is the rewrite's own
    added = relay + '\n\ndef more(chunk):\n    return hasattr(chunk.choices[0].delta, "content")\n'
    assert len(stream_check_errors("chatpad/relay.py", relay, added)) == 1
    assert stream_check_errors("notes.txt", "a", "b") == []
    assert stream_check_errors(GENERATE, ORIGINAL, "def broken(:\n") == []


def test_check_candidate_records_the_streaming_check():
    findings = scan(FIXTURE, "openai-v1").findings
    own = [f for f in findings if f.file == GENERATE]

    def checked(candidate: str):
        return check_candidate(
            GENERATE,
            ORIGINAL,
            candidate,
            required=True,
            library_modules=MIGRATION.api_modules,
            known_imports=MIGRATION.known_imports,
            findings=own,
            rewrite_checks=MIGRATION.rewrite_checker(FIXTURE, findings),
        )

    good = checked(migrated())
    assert good.ok and good.checks["streaming"] == "ok" and good.checks["config"] == "ok"
    bad = checked(migrated('if hasattr(delta, "content"):'))
    assert bad.error_checks == ["streaming"]
    assert bad.checks["streaming"] == bad.errors[0]


# --- whole-file mode ----------------------------------------------------------------------------

MIGRATED_TESTS = (
    (FIXTURE / TESTS)
    .read_text()
    .replace(
        "from unittest import mock\n",
        "from types import SimpleNamespace\nfrom unittest import mock\n",
    )
    .replace(
        '    return {"choices": [{"message": {"role": "assistant", "content": text}}]}\n',
        '    message = SimpleNamespace(role="assistant", content=text)\n'
        "    return SimpleNamespace(choices=[SimpleNamespace(message=message)])\n",
    )
    .replace(
        '@mock.patch("openai.ChatCompletion.create")\ndef test_generate_reply(create):\n'
        '    create.return_value = completion("hello")',
        '@mock.patch("chatpad.generate.OpenAI")\ndef test_generate_reply(OpenAI):\n'
        '    OpenAI.return_value.chat.completions.create.return_value = completion("hello")',
    )
    .replace(
        '@mock.patch("openai.ChatCompletion.create")\ndef test_summarize(create):\n'
        '    create.return_value = completion("short")',
        '@mock.patch("chatpad.generate.OpenAI")\ndef test_summarize(OpenAI):\n'
        '    OpenAI.return_value.chat.completions.create.return_value = completion("short")',
    )
)


def _judge(tree: Path):
    outcomes = ("tests.test_generate::test_generate_reply", "tests.test_generate::test_summarize")
    text = (tree / GENERATE).read_text()
    lines = frozenset(n for n, line in enumerate(text.splitlines(), 1) if line.strip())
    coverage = {GENERATE: FileCoverage(lines, frozenset())}
    return run(*((t, "passed") for t in outcomes), coverage=coverage)


def test_whole_file_sends_streaming_only_with_the_streaming_file(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    llm = FakeLLM(
        [
            block(migrated('if hasattr(delta, "content"):')),  # rejected by the check
            block(migrated()),  # the check retry
            block(MIGRATED_TESTS),
        ]
    )
    result = wf(repo, llm, JudgingSandbox(judge=_judge), max_retries=0)
    assert result.outcome in ("verified", "verified_weak"), result.trajectory["reason"]
    assert "## Streaming" not in llm.systems[0]
    assert "streaming" not in result.trajectory["job"]["guide_sections"]
    first, retry, tests = (r[0].text for r in llm.requests)
    assert "Migration guide for this file:\n\n# openai 0.x -> 1.x\n\n## Streaming" in first
    assert "## Streaming" in retry
    assert "## Streaming" not in tests and "Migration guide for this file" not in tests
    assert '`hasattr(delta, "content")` must become `if delta.content is not None:`' in retry
    entry = result.trajectory["attempts"][0]["files"][0]
    assert entry["path"] == GENERATE and entry["accepted"]
    assert [r["checks"]["streaming"] == "ok" for r in entry["replies"]] == [False, True]
