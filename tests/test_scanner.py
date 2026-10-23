from __future__ import annotations

import os
import textwrap

import pytest

from apifix.engine.scanner import scan
from tests.conftest import FIXTURES, at, summary

# --- realistic fixtures ---------------------------------------------------------------------


def test_chatbot(fixture_findings):
    findings = fixture_findings("chatbot.py")
    assert summary(findings) == [
        (9, "client_config"),
        (20, "api_call"),
        (24, "api_call"),
        (26, "response_access"),
        (27, "response_access"),
        (28, "error_class"),
    ]
    call = at(findings, 20)
    assert call.old_pattern == "openai.ChatCompletion.create"
    assert "client.chat.completions.create" in call.suggestion
    assert call.col == 20 and call.confidence == "certain"
    assert "timeout=" in at(findings, 24).suggestion
    assert at(findings, 27).old_pattern == 'resp["choices"][0]["message"]["content"]'
    assert at(findings, 27).confidence == "likely"
    assert at(findings, 28).suggestion == "Use openai.RateLimitError"
    # openai.api_key = os.getenv("OPENAI_API_KEY"): the variable 1.x reads itself
    assert at(findings, 9).old_pattern == 'openai.api_key = os.getenv("OPENAI_API_KEY")'
    assert at(findings, 9).suggestion.startswith("1.x reads OPENAI_API_KEY itself")


def test_async_streaming(fixture_findings):
    findings = fixture_findings("async_streaming.py")
    assert summary(findings) == [
        (10, "api_call"),
        (16, "response_access"),
        (17, "response_access"),
    ]
    assert "await client.chat.completions.create" in at(findings, 10).suggestion
    assert "AsyncOpenAI" in at(findings, 10).suggestion
    assert "delta.content" in at(findings, 16).suggestion
    assert at(findings, 17).old_pattern == 'delta.get("content", "")'


def test_embeddings_search(fixture_findings):
    findings = fixture_findings("embeddings_search.py")
    assert summary(findings) == [
        (4, "removed_module"),
        (10, "api_call"),
        (10, "api_call"),
        (11, "response_access"),
        (15, "removed_module"),
        (16, "removed_module"),
    ]
    call, kwarg = [f for f in findings if f.line == 10]
    assert "client.embeddings.create" in call.suggestion
    assert kwarg.old_pattern == 'engine="text-embedding-ada-002"'
    assert "model=" in kwarg.suggestion


def test_azure_config_suggests_azure_client(fixture_findings):
    findings = fixture_findings("azure_config.py")
    assert summary(findings) == [
        (7, "client_config"),
        (8, "client_config"),
        (9, "client_config"),
        (10, "client_config"),
        (14, "api_call"),
        (15, "api_call"),
    ]
    assert "azure_endpoint" in at(findings, 8).suggestion
    assert "base_url" not in at(findings, 8).suggestion
    for f in findings:
        if f.category == "client_config":
            assert "AzureOpenAI(azure_endpoint=..., api_version=..." in f.suggestion
    assert "AzureOpenAI" in at(findings, 14).suggestion
    assert "model=" in at(findings, 15).suggestion


def test_aliased_imports(fixture_findings):
    findings = fixture_findings("aliased_imports.py")
    assert summary(findings) == [
        (4, "import"),
        (5, "import"),
        (5, "import"),
        (5, "import"),
        (10, "api_call"),
        (11, "error_class"),
        (13, "error_class"),
        (15, "response_access"),
        (19, "api_call"),
        (20, "response_access"),
    ]
    invalid, ratelimit, timeout = [f for f in findings if f.line == 5]
    assert invalid.suggestion.startswith("from openai import BadRequestError")
    assert ratelimit.suggestion == "from openai import RateLimitError"
    assert timeout.suggestion == "from openai import APITimeoutError"
    # `except (RateLimitError, Timeout)`: only Timeout changes name, so only it is flagged.
    assert at(findings, 13).old_pattern == "Timeout"
    assert at(findings, 10).old_pattern == "CC.create"
    assert "client.moderations.create" in at(findings, 19).suggestion


def test_invalid_request_error_is_likely_and_explains_split(fixture_findings):
    f = at(fixture_findings("aliased_imports.py"), 11)
    assert f.confidence == "likely"
    assert "BadRequestError" in f.suggestion
    assert "NotFoundError" in f.suggestion and "several classes" in f.suggestion


def test_legacy_finetune_is_removed_not_renamed(fixture_findings):
    findings = fixture_findings("legacy_finetune.py")
    legacy, job = findings
    assert legacy.line == 7
    assert "removed" in legacy.suggestion
    assert "migrate manually to client.fine_tuning.jobs (different params)" in legacy.suggestion
    assert not legacy.suggestion.startswith("Use ")
    assert job.line == 11
    assert job.suggestion.startswith("Use client.fine_tuning.jobs.create(")


@pytest.mark.parametrize("name", ["already_v1.py", "unrelated.py"])
def test_no_false_positives(fixture_findings, name):
    assert fixture_findings(name) == []


# --- directory scanning ---------------------------------------------------------------------


def test_fixture_directory_scan():
    result = scan(FIXTURES, "openai-v1")
    assert [e.file for e in result.parse_errors] == ["broken_syntax.py"]
    assert "SyntaxError" in result.parse_errors[0].error
    assert result.files_scanned == 9
    assert result.findings == sorted(result.findings)
    assert {f.file for f in result.findings} == {
        "aliased_imports.py",
        "async_streaming.py",
        "azure_config.py",
        "chatbot.py",
        "embeddings_search.py",
        "legacy_finetune.py",
    }


def test_scan_is_deterministic():
    assert scan(FIXTURES, "openai-v1") == scan(FIXTURES, "openai-v1")


OLD_CALL = "import openai\nopenai.Completion.create(prompt='hi')\n"


def test_skips_virtualenvs_and_caches(tmp_path):
    for d in [".venv/lib", "node_modules/x", "build", "myenv", "pkg"]:
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "mod.py").write_text(OLD_CALL)
    (tmp_path / "myenv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    result = scan(tmp_path, "openai-v1")
    assert {f.file for f in result.findings} == {"pkg/mod.py"}


def test_skips_pixi_and_conda_environments(tmp_path):
    # conda environments (pixi's too) have no pyvenv.cfg, and their stdlib is not under
    # site-packages
    for d in [".pixi/envs/default/lib/python3.11", "condaenv/lib/python3.11", "pkg"]:
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "mod.py").write_text(OLD_CALL)
    (tmp_path / "condaenv" / "conda-meta").mkdir()
    result = scan(tmp_path, "openai-v1")
    assert {f.file for f in result.findings} == {"pkg/mod.py"}


def test_paths_are_relative_posix(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c.py").write_text(OLD_CALL)
    assert scan(tmp_path, "openai-v1").findings[0].file == "a/b/c.py"


def test_non_utf8_file_is_recorded_and_scan_continues(tmp_path):
    (tmp_path / "latin1.py").write_bytes(b"import openai\nx = '\xe9t\xe9'\n")
    (tmp_path / "ok.py").write_text(OLD_CALL)
    result = scan(tmp_path, "openai-v1")
    assert [e.file for e in result.parse_errors] == ["latin1.py"]
    assert "UnicodeDecodeError" in result.parse_errors[0].error
    assert {f.file for f in result.findings} == {"ok.py"}


def test_coding_cookie_is_honoured(tmp_path):
    (tmp_path / "latin1.py").write_bytes(
        b"# -*- coding: latin-1 -*-\nimport openai\nx = '\xe9'\nopenai.Completion.create()\n"
    )
    result = scan(tmp_path, "openai-v1")
    assert result.parse_errors == []
    assert [f.line for f in result.findings] == [4]


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinks_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text(OLD_CALL)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text(OLD_CALL)
    (repo / "linked_dir").symlink_to(outside, target_is_directory=True)
    (repo / "linked_file.py").symlink_to(outside / "secret.py")
    (repo / "loop").symlink_to(repo, target_is_directory=True)
    result = scan(repo, "openai-v1")
    assert {f.file for f in result.findings} == {"real.py"}
    assert result.files_scanned == 1


def test_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        scan(tmp_path / "nope", "openai-v1")


# --- targeted snippets ----------------------------------------------------------------------


def test_import_forms(scan_code):
    findings = scan_code(
        """
        import openai.error
        import openai.error as oe
        from openai import error
        from openai import InvalidRequestError
        import openai.embeddings_utils
        """
    )
    assert summary(findings) == [
        (2, "import"),
        (3, "import"),
        (4, "import"),
        (5, "import"),
        (6, "removed_module"),
    ]
    assert "BadRequestError" in at(findings, 5).suggestion


def test_attribute_via_aliased_error_module(scan_code):
    findings = scan_code(
        """
        import openai.error as oe

        try:
            pass
        except oe.ServiceUnavailableError:
            pass
        """
    )
    assert at(findings, 6).suggestion == "Use openai.InternalServerError"


def test_removed_errors(scan_code):
    findings = scan_code(
        """
        import openai
        try:
            pass
        except openai.error.TryAgain:
            pass
        """
    )
    assert "retries automatically" in at(findings, 5).suggestion


def test_top_level_invalid_request_error(scan_code):
    findings = scan_code(
        "import openai\ntry:\n    pass\nexcept openai.InvalidRequestError:\n    pass\n"
    )
    assert at(findings, 4, "error_class").confidence == "likely"


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("openai.Image.create(prompt='x')", "client.images.generate("),
        ("openai.Image.create_edit(image=f)", "client.images.edit("),
        ("openai.Image.create_variation(image=f)", "client.images.create_variation("),
        ("openai.Audio.transcribe('whisper-1', f)", "client.audio.transcriptions.create("),
        ("openai.Audio.translate('whisper-1', f)", "client.audio.translations.create("),
        ("openai.File.download('file-1')", "client.files.content("),
        ("openai.Model.list()", "client.models.list("),
        ("openai.Completion.create(prompt='x')", "client.completions.create("),
        ("openai.FineTuningJob.list_events(id='x')", "client.fine_tuning.jobs.list_events("),
    ],
)
def test_resource_method_mapping(scan_code, call, expected):
    findings = scan_code(f"import openai\nf = None\nopenai.{call.removeprefix('openai.')}\n")
    assert expected in at(findings, 3, "api_call").suggestion


@pytest.mark.parametrize("resource", ["Edit", "Engine", "Customer", "FineTune"])
def test_removed_resources(scan_code, resource):
    findings = scan_code(f"import openai\nopenai.{resource}.create()\n")
    assert "emoved" in at(findings, 2).suggestion


@pytest.mark.parametrize(
    ("attr", "expected", "confidence"),
    [
        ("api_base", "base_url", "certain"),
        ("proxy", "http_client", "certain"),
        ("api_key_path", "api_key=", "certain"),
        ("organization", "organization=", "likely"),
    ],
)
def test_module_config(scan_code, attr, expected, confidence):
    f = at(scan_code(f"import openai\nopenai.{attr} = 'x'\n"), 2)
    assert f.category == "client_config"
    assert expected in f.suggestion
    assert f.confidence == confidence


def test_per_call_credentials(scan_code):
    findings = scan_code("import openai\nopenai.Completion.create(api_key='k', headers={})\n")
    kwargs = [f for f in findings if f.old_pattern.startswith(("api_key", "headers"))]
    assert len(kwargs) == 2
    assert any("extra_headers" in f.suggestion for f in kwargs)


def test_direct_subscript_on_call(scan_code):
    findings = scan_code(
        """
        import openai
        text = openai.Completion.create(prompt="x")["choices"][0]["text"]
        """
    )
    assert summary(findings) == [(3, "api_call"), (3, "response_access")]


def test_get_after_subscript_is_reported_once(scan_code):
    findings = scan_code(
        """
        import openai
        r = openai.ChatCompletion.create(model="m", messages=[])
        msg = r["choices"][0].get("message")
        """
    )
    assert summary(findings) == [(3, "api_call"), (4, "response_access")]


def test_json_dumps_and_to_dict(scan_code):
    findings = scan_code(
        """
        import json
        import openai
        r = openai.Moderation.create(input="x")
        json.dumps(r)
        r.to_dict()
        """
    )
    assert [f.line for f in findings if f.category == "response_access"] == [5, 6]
    assert "model_dump" in at(findings, 5).suggestion


def test_attribute_access_on_response_is_fine(scan_code):
    findings = scan_code(
        """
        import openai
        r = openai.ChatCompletion.create(model="m", messages=[])
        print(r.choices[0].message.content)
        """
    )
    assert summary(findings) == [(3, "api_call")]


def test_reassignment_stops_tracking(scan_code):
    findings = scan_code(
        """
        import openai
        r = openai.ChatCompletion.create(model="m", messages=[])
        r = {"choices": []}
        r["choices"]
        """
    )
    assert summary(findings) == [(3, "api_call")]


def test_response_tracking_is_scoped_per_function(scan_code):
    findings = scan_code(
        """
        import openai

        def a():
            r = openai.ChatCompletion.create(model="m", messages=[])
            return r

        def b(r):
            return r["choices"]
        """
    )
    assert summary(findings) == [(5, "api_call")]


def test_non_stream_iteration_does_not_mark_items(scan_code):
    findings = scan_code(
        """
        import openai
        models = openai.Model.list()
        for m in [1, 2]:
            m["id"]
        """
    )
    assert summary(findings) == [(3, "api_call")]


def test_mock_patch_string_targets(scan_code):
    findings = scan_code(
        """
        from unittest import mock

        @mock.patch("openai.ChatCompletion.create")
        @mock.patch("openai.error.Timeout")
        def test_x(m1, m2):
            mock.patch("openai.api_key")
            "openai.ChatCompletion.create is used in prose here"
        """
    )
    assert summary(findings) == [(4, "api_call"), (5, "error_class")]
    assert all(f.confidence == "likely" for f in findings)
    assert "mock.patch" in at(findings, 4).suggestion


def test_string_targets_through_a_module_prefix(scan_code):
    findings = scan_code(
        """
        from unittest import mock

        @mock.patch("server.openai.Completion.create")
        @mock.patch("src.svc.core.openai.ChatCompletion.acreate")
        @mock.patch("svc.server.openai.error.RateLimitError")
        def test_x(m1, m2, m3):
            mock.patch("server.openai.api_key")  # config, not an API call: as without a prefix
            mock.patch("server.openai_helpers.Completion.create")  # not the openai module
            mock.patch("server.openai")
            "server.openai.Completion.create is mentioned in prose"
        """
    )
    assert summary(findings) == [(4, "api_call"), (5, "api_call"), (6, "error_class")]
    assert all(f.confidence == "likely" for f in findings)
    first = at(findings, 4).suggestion
    assert "through module server" in first and "server.openai.OpenAI" in first
    assert "client.completions.create" in first


def test_prefixed_string_naming_the_repos_own_openai_module_is_ignored(tmp_path):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "__init__.py").write_text("")
    (tmp_path / "server" / "openai.py").write_text("class Completion:\n    pass\n")
    (tmp_path / "test_server.py").write_text(
        'from unittest import mock\n\n@mock.patch("server.openai.Completion.create")\n'
        "def test_x(m):\n    pass\n"
    )
    assert [f for f in scan(tmp_path, "openai-v1").findings if f.file == "test_server.py"] == []


def test_shadowed_names_without_openai_import(scan_code):
    findings = scan_code(
        """
        class error:
            RateLimitError = Exception

        try:
            pass
        except error.RateLimitError:
            pass
        """
    )
    assert findings == []


def test_long_patterns_are_truncated(scan_code):
    code = "import openai\nr = openai.Completion.create(prompt='x')\nr" + '["choices"]' * 20 + "\n"
    f = at(scan_code(code), 3)
    assert len(f.old_pattern) <= 120 and f.old_pattern.endswith("...")


# --- test files: dict-shaped mock responses --------------------------------------------------


def _scan_file(tmp_path, rel: str, code: str) -> list:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(code))
    return [f for f in scan(tmp_path, "openai-v1").findings if f.file == rel]


MOCK_DICT = (
    "1.x responses are objects: build mock responses with attribute access "
    "(e.g. SimpleNamespace), not dicts."
)


@pytest.mark.parametrize(
    ("rel", "header"),
    [
        ("svc/testserver.py", ""),  # unittest's test*.py, not pytest's test_*.py
        ("svc/checks.py", "import unittest\n\nclass ServerChecks(unittest.TestCase):\n    pass\n"),
    ],
)
def test_dict_response_in_test_files_by_new_name_or_content(tmp_path, rel, header):
    (f,) = _scan_file(
        tmp_path,
        rel,
        header + 'REPLY = {"choices": [{"message": {"content": "hi"}}]}\n',
    )
    assert (f.category, f.suggestion) == ("response_access", MOCK_DICT)


def test_dict_response_in_a_non_test_module_is_not_flagged(tmp_path):
    assert _scan_file(tmp_path, "svc/fixtures.py", 'REPLY = {"choices": [{"text": "hi"}]}\n') == []


def test_dict_response_in_a_test_helper_is_likely(tmp_path):
    (f,) = _scan_file(
        tmp_path,
        "tests/test_app.py",
        """
        def completion(text):
            return {"choices": [{"message": {"content": text}}]}
        """,
    )
    assert (f.line, f.category, f.confidence) == (3, "response_access", "likely")
    assert f.old_pattern == '{"choices": [{"message": {"content": text}}]}'
    assert f.suggestion == MOCK_DICT


def test_inline_and_completion_shaped_dicts_are_flagged(tmp_path):
    findings = _scan_file(
        tmp_path,
        "test_bot.py",
        """
        from unittest import mock

        @mock.patch("bot.client.completions.create")
        def test_complete(create):
            create.return_value = {"choices": ({"text": "hi"},), "usage": {}}
            data = {"model": "m", "choices": [{"index": 0, "message": {"content": "x"}}]}
        """,
    )
    assert [(f.line, f.suggestion) for f in findings] == [(6, MOCK_DICT), (7, MOCK_DICT)]


def test_other_dicts_and_non_test_files_are_not_flagged(tmp_path):
    assert (
        _scan_file(
            tmp_path,
            "tests/test_misc.py",
            """
        a = {"choices": ["red", "green"]}
        b = {"choices": [{"label": "x"}]}
        c = {"message": {"content": "x"}}
        d = dict(choices=[{"message": {}}])
        """,
        )
        == []
    )
    assert (
        _scan_file(
            tmp_path, "app/fixtures.py", 'SAMPLE = {"choices": [{"message": {"content": "x"}}]}\n'
        )
        == []
    )


def test_tiny_repo_test_file_gets_the_new_finding(tmp_path):
    from tests.integration.tiny_repo import ORIGINAL as TINY

    for rel, text in TINY.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(textwrap.dedent(text))
    tests = [f for f in scan(tmp_path, "openai-v1").findings if f.file == "tests/test_app.py"]
    assert [(f.line, f.category) for f in tests] == [
        (7, "response_access"),
        (10, "api_call"),
        (16, "api_call"),
    ]


# --- module configuration: statement-level findings and the repo-level pass ---------------------


@pytest.mark.parametrize(
    ("statement", "start", "confidence"),
    [
        (
            'openai.api_key = os.getenv("OPENAI_API_KEY")',
            "1.x reads OPENAI_API_KEY itself",
            "likely",
        ),
        (
            'openai.api_key = os.environ["OPENAI_API_KEY"]',
            "1.x reads OPENAI_API_KEY itself",
            "likely",
        ),
        ("openai.api_key = None", "1.x reads OPENAI_API_KEY itself", "likely"),
        ('openai.organization = os.getenv("OPENAI_ORG_ID")', "1.x reads OPENAI_ORG_ID", "likely"),
        ("openai.api_key = key", "Never drop this value", "likely"),
        ('openai.api_key = os.getenv("MY_KEY")', "Never drop this value", "likely"),
        ('openai.api_key = os.getenv("OPENAI_API_KEY", "x")', "Never drop this value", "likely"),
        ('openai.api_key = "EMPTY"', "Never drop this value", "likely"),
        ("openai.api_base = url", "1.x ignores api_base", "certain"),
        ('openai.api_base = os.getenv("OPENAI_API_BASE")', "1.x ignores api_base", "certain"),
    ],
)
def test_config_assignments_are_reported_on_the_statement(scan_code, statement, start, confidence):
    (f,) = scan_code(f"import os\nimport openai\n\nkey = url = None\n{statement}\n")
    assert (f.line, f.col, f.category, f.old_pattern) == (5, 1, "client_config", statement)
    assert f.suggestion.startswith(start) and f.confidence == confidence


def test_config_suggestions_name_the_value(scan_code):
    (key,) = scan_code("import openai\n\n\ndef main(cli_key):\n    openai.api_key = cli_key\n")
    assert key.suggestion == (
        "Never drop this value: keep the assignment and call the module-level client "
        "(openai.chat.completions.create(...) reads openai.api_key), or pass it to every client: "
        "openai.OpenAI(api_key=cli_key)"
    )
    (base,) = scan_code("import openai as oa\noa.api_base = 'http://localhost:8000/v1'\n")
    assert base.old_pattern == "oa.api_base = 'http://localhost:8000/v1'"
    assert base.suggestion == (
        "1.x ignores api_base: assign openai.base_url = 'http://localhost:8000/v1' and use the "
        "module-level client, or pass openai.OpenAI(base_url='http://localhost:8000/v1'); never "
        "drop it"
    )


def test_config_reads_get_a_neutral_hint(scan_code):
    findings = scan_code("import openai\n\nif not openai.api_key:\n    raise SystemExit(1)\n")
    assert [(f.line, f.old_pattern) for f in findings] == [(3, "openai.api_key")]
    assert findings[0].suggestion == (
        "1.x keeps openai.api_key for the module-level client only; openai.OpenAI() does not "
        "read it"
    )


def test_azure_config_keeps_its_suggestions_and_never_drops_values(fixture_findings):
    findings = fixture_findings("azure_config.py")
    base, key = at(findings, 8), at(findings, 10)
    assert base.old_pattern == 'openai.api_base = os.environ["AZURE_OPENAI_ENDPOINT"]'
    assert base.suggestion.startswith("api_base becomes azure_endpoint")
    assert base.suggestion.endswith("; never drop the value")
    assert key.suggestion.endswith("; never drop the value")
    assert "client = openai.AzureOpenAI" in at(findings, 14).suggestion  # no repo-level rewrite


def _repo_findings(tmp_path, files: dict[str, str]) -> list:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(textwrap.dedent(text))
    return scan(tmp_path, "openai-v1").findings


def test_split_config_points_call_sites_at_the_module_level_client():
    findings = scan(FIXTURES.parent / "split_config", "openai-v1").findings
    by = {(f.file, f.line): f for f in findings}
    assert by[("voicebot/assistant.py", 31)].suggestion == (
        "Use openai.chat.completions.create(...): the module-level client, which reads "
        "openai.api_key and openai.base_url (set in voicebot/cli.py:12,13); the response is a "
        "typed object, not a dict"
    )
    assert by[("voicebot/cli.py", 12)].suggestion.endswith(
        ". The API calls are in voicebot/assistant.py: keep this assignment"
    )
    assert by[("voicebot/cli.py", 13)].suggestion.endswith(
        ". The API calls are in voicebot/assistant.py: keep the value set here (as openai.base_url)"
    )
    assert by[("tests/test_assistant.py", 10)].suggestion == (
        'String reference (e.g. a mock.patch target): patch "openai.chat.completions.create" '
        "(the code uses the module-level client; the test needs OPENAI_API_KEY or "
        "openai.api_key set)"
    )


def test_repo_pass_async_calls_and_untouched_cases(tmp_path):
    findings = _repo_findings(
        tmp_path,
        {
            "app/config.py": "import openai\n\n\ndef setup(key):\n    openai.api_key = key\n",
            "app/chat.py": """\
                import openai


                async def ask(m):
                    return await openai.ChatCompletion.acreate(model="m", messages=m)
                """,
        },
    )
    (call,) = [f for f in findings if f.category == "api_call"]
    assert call.suggestion == (
        "Use await client.chat.completions.create(...) with client = "
        "openai.AsyncOpenAI(api_key=openai.api_key) created where it is used (1.x has no "
        "module-level async client); the response is a typed object, not a dict"
    )


CONFIG_FROM_ENV = 'import os\nimport openai\n\nopenai.api_key = os.getenv("OPENAI_API_KEY")\n'
CHAT = "import openai\n\nopenai.ChatCompletion.create(model='m', messages=[])\n"


def test_no_repo_pass_when_the_key_comes_from_the_environment(tmp_path):
    findings = _repo_findings(
        tmp_path,
        {
            "app/config.py": CONFIG_FROM_ENV,
            "app/chat.py": CHAT,
            "tests/test_chat.py": "from unittest import mock\n\nmock.patch(CREATE)\n".replace(
                "CREATE", "'openai.ChatCompletion.create'"
            ),
        },
    )
    by = {f.file: f for f in findings}
    assert by["app/chat.py"].suggestion.startswith("Use client.chat.completions.create(...) with")
    assert "keep this assignment" not in by["app/config.py"].suggestion
    assert "client.chat.completions.create" in by["tests/test_chat.py"].suggestion


def test_a_test_file_setting_a_key_does_not_trigger_the_repo_pass(tmp_path):
    findings = _repo_findings(
        tmp_path,
        {
            "tests/test_chat.py": "import openai\n\nopenai.api_key = 'test'\n",
            "app/chat.py": CHAT,
        },
    )
    call = next(f for f in findings if f.file == "app/chat.py")
    assert call.suggestion.startswith("Use client.chat.completions.create(...) with")
