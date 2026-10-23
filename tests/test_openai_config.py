"""openai-v1 config checks for whole-file rewrites: dropped (A) and ignored (B) module config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apifix.engine.migrations import get_migration
from apifix.engine.scanner import scan

MIGRATION = get_migration("openai-v1")
FIXTURE = Path(__file__).parent / "fixtures" / "split_config"

CLI = """\
import os

import openai

from app.bot import ask


def main(key, url=None):
    openai.api_key = key
    openai.api_base = url
    print(ask("hi"))
"""
BOT = """\
import openai


def ask(q):
    r = openai.ChatCompletion.create(model="m", messages=[{"role": "user", "content": q}])
    return r["choices"][0]["message"]["content"]
"""
BOT_MODULE_CLIENT = BOT.replace("openai.ChatCompletion.create", "openai.chat.completions.create")


def make(tmp_path: Path, files: dict[str, str]):
    root = tmp_path / "repo"
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    checker = MIGRATION.rewrite_checker(root, scan(root, MIGRATION).findings)
    assert checker is not None

    def check(path: str, candidate: str) -> list[str]:
        return checker(path, (root / path).read_text(), textwrap.dedent(candidate))

    return check


def edit(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


# --- check A: dropped ---------------------------------------------------------------------------


def test_dropped_key_names_both_options_and_where_the_calls_are(tmp_path):
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT})
    candidate = edit(CLI, "    openai.api_key = key\n", "    # OpenAI() reads the key itself\n")
    candidate = edit(candidate, "openai.api_base = url", "openai.base_url = url")
    assert check("app/cli.py", candidate) == [
        "original line 9: removed `openai.api_key = key`, so that key would be silently ignored "
        "in 1.x. Keep the assignment and call the module-level client "
        "(`openai.chat.completions.create(...)` reads openai.api_key), or pass the value to every "
        "client (`openai.OpenAI(api_key=key)`). This file makes no API calls (they are in "
        "app/bot.py): keep the assignment"
    ]


def test_kept_or_passed_to_a_client_is_fine(tmp_path):
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT})
    kept = edit(CLI, "openai.api_base = url", "openai.base_url = url")
    assert check("app/cli.py", kept) == []
    passed = edit(
        CLI,
        "    openai.api_key = key\n    openai.api_base = url\n",
        "    client = openai.OpenAI(api_key=key, base_url=url)\n",
    )
    assert check("app/cli.py", passed) == []


def test_api_base_kept_as_is_has_no_effect(tmp_path):
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT})
    assert check("app/cli.py", CLI) == [
        "original line 10: `openai.api_base = url` has no effect in 1.x (nothing reads "
        "api_base). Assign `openai.base_url = url` and call the module-level client, or pass "
        "it: `openai.OpenAI(base_url=url)`"
    ]
    dropped = edit(CLI, "    openai.api_base = url\n", "")
    (error,) = check("app/cli.py", dropped)
    assert error.startswith(
        "original line 10: removed `openai.api_base = url`, so requests would go to "
        "api.openai.com in 1.x. Assign `openai.base_url = url` and call the module-level client"
    )
    assert "`openai.OpenAI(base_url=url)`" in error


def test_a_value_passed_through_a_variable_is_not_recognised(tmp_path):
    """Documented false positive: only the same expression counts."""
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT})
    candidate = edit(
        CLI,
        "    openai.api_key = key\n    openai.api_base = url\n",
        "    k = key\n    client = openai.OpenAI(api_key=k, base_url=url)\n",
    )
    (error,) = check("app/cli.py", candidate)
    assert error.startswith("original line 9: removed `openai.api_key = key`")


@pytest.mark.parametrize(
    ("assignment", "dropped_ok"),
    [
        ('openai.api_key = os.getenv("OPENAI_API_KEY")', True),
        ('openai.api_key = os.environ["OPENAI_API_KEY"]', True),
        ('openai.api_key = os.environ.get("OPENAI_API_KEY")', True),
        ('openai.organization = os.getenv("OPENAI_ORG_ID")', True),
        ("openai.api_key = None", True),
        ('openai.api_key = os.getenv("OPENAI_API_KEY", "")', False),  # a default
        ('openai.api_key = os.getenv("MY_KEY")', False),
        ('openai.api_base = os.getenv("OPENAI_API_BASE")', False),  # 1.x reads OPENAI_BASE_URL
        ('openai.api_key = "EMPTY"', False),  # literals too (a local server's dummy key)
        ('openai.api_base = "http://localhost:8000/v1"', False),
    ],
)
def test_values_1x_reads_itself_may_go(tmp_path, assignment, dropped_ok):
    source = f"import os\n\nimport openai\n\n{assignment}\n"
    check = make(tmp_path, {"app/config.py": source})
    errors = check("app/config.py", "import os\n\nimport openai\n")
    assert (errors == []) is dropped_ok, errors


def test_aliases_are_resolved(tmp_path):
    source = "import openai as oa\n\n\ndef setup(key):\n    oa.api_key = key\n"
    check = make(tmp_path, {"app/config.py": source})
    (error,) = check("app/config.py", "import openai as oa\n\n\ndef setup(key):\n    pass\n")
    assert error.startswith("original line 5: removed `oa.api_key = key`")
    kept = "import openai as oa\n\n\ndef setup(key):\n    oa.api_key = key\n"
    assert check("app/config.py", kept) == []


def test_test_files_are_not_checked(tmp_path):
    test = "import openai\n\nopenai.api_key = 'test'\n\n\ndef test_x():\n    assert openai\n"
    check = make(tmp_path, {"tests/test_x.py": test, "app/cli.py": CLI})
    assert (
        check("tests/test_x.py", "import openai\n\n\ndef test_x():\n    c = openai.OpenAI()\n")
        == []
    )


# --- check B: ignored ---------------------------------------------------------------------------


def test_a_new_client_that_ignores_the_configured_key(tmp_path):
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT})
    candidate = edit(
        BOT,
        "    r = openai.ChatCompletion.create(",
        "    client = openai.OpenAI()\n    r = client.chat.completions.create(",
    )
    assert check("app/bot.py", candidate) == [
        "line 5: `openai.OpenAI()` does not read openai.api_key, which app/cli.py:9 sets from "
        "`key` or openai.base_url, which app/cli.py:10 sets from `url` (as openai.api_base), so "
        "that configuration would be ignored. Call the module-level client instead "
        "(`openai.chat.completions.create(...)`, no client object), or pass it: "
        "`openai.OpenAI(api_key=openai.api_key, base_url=openai.base_url)`"
    ]


def test_the_fixture_s_client_message(tmp_path):
    files = {p.relative_to(FIXTURE).as_posix(): p.read_text() for p in FIXTURE.rglob("*.py")}
    check = make(tmp_path, files)
    assistant = files["voicebot/assistant.py"]
    candidate = edit(
        assistant,
        "        response = openai.ChatCompletion.create(",
        "        response = openai.OpenAI(api_key=openai.api_key).chat.completions.create(",
    )
    (error,) = check("voicebot/assistant.py", candidate)
    assert error.startswith(
        "line 31: `openai.OpenAI(api_key=openai.api_key)` does not read openai.base_url, which "
        "voicebot/cli.py:13 sets from `base_url` (as openai.api_base), so that base URL would "
        "be ignored."
    )


@pytest.mark.parametrize(
    "call",
    [
        "openai.OpenAI(api_key=openai.api_key, base_url=openai.base_url)",
        "openai.OpenAI(**settings)",
    ],
)
def test_clients_that_pass_the_values_are_fine(tmp_path, call):
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT})
    candidate = edit(
        BOT,
        "    r = openai.ChatCompletion.create(",
        f"    settings = {{}}\n    client = {call}\n    r = client.chat.completions.create(",
    )
    assert check("app/bot.py", candidate) == []


def test_the_module_level_client_is_fine(tmp_path):
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT})
    assert check("app/bot.py", BOT_MODULE_CLIENT) == []


def test_no_check_b_without_a_configured_value(tmp_path):
    cli = CLI.replace("openai.api_key = key", 'openai.api_key = os.getenv("OPENAI_API_KEY")')
    cli = cli.replace("    openai.api_base = url\n", "")
    check = make(tmp_path, {"app/cli.py": cli, "app/bot.py": BOT})
    candidate = edit(
        BOT,
        "    r = openai.ChatCompletion.create(",
        "    client = openai.OpenAI()\n    r = client.chat.completions.create(",
    )
    assert check("app/bot.py", candidate) == []


def test_no_check_b_in_test_files(tmp_path):
    test = (
        "from unittest import mock\n\n\n@mock.patch('openai.ChatCompletion.create')\n"
        "def test_x(create):\n    assert create\n"
    )
    check = make(tmp_path, {"app/cli.py": CLI, "app/bot.py": BOT, "tests/test_x.py": test})
    candidate = (
        "import openai\nfrom unittest import mock\n\n\n@mock.patch('openai.OpenAI')\n"
        "def test_x(OpenAI):\n    assert openai.OpenAI() is OpenAI.return_value\n"
    )
    assert check("tests/test_x.py", candidate) == []
