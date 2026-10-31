"""Check accuracy on rewrites shaped like gemini_provider.py (the key reaching the new client
through `or` / a conditional / a local; old-response names read as attributes)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apifix.engine.agent import revert_text
from apifix.engine.migrations import get_migration
from apifix.engine.scanner import scan
from apifix.engine.unrelated import revert_text_edits, unrelated_edits

MIGRATION = get_migration("google-genai")
LIBRARY = frozenset({"google.generativeai", "google"})

ORIGINAL = """\
import os
import google.generativeai as genai


class Provider:
    def __init__(self, api_key):
        self._api_key = api_key
        genai.configure(api_key=self._api_key)

    def text(self, response):
        text = ""
        if hasattr(response, "text"):
            try:
                text = response.text
            except ValueError as e:
                if hasattr(response, "candidates") and response.candidates:
                    candidate = response.candidates[0]
                    finish_reason = getattr(candidate, "finish_reason", None)
                    if finish_reason == 2:
                        raise RuntimeError("blocked by safety filters") from e
                raise
        return text
"""


def key_errors(candidate: str) -> list[str]:
    return MIGRATION.rewrite_checker(Path("."), [])("app.py", ORIGINAL, candidate)["key"]


def init(body: str) -> str:
    head, tail = ORIGINAL.split("        genai.configure(api_key=self._api_key)\n")
    return head.replace("import google.generativeai as genai", "from google import genai") + (
        body + tail
    )


@pytest.mark.parametrize(
    "body",
    [
        "        self._client = genai.Client(api_key=self._api_key)\n",
        "        self._client = genai.Client(\n"
        '            api_key=self._api_key or os.getenv("GEMINI_API_KEY")\n        )\n',
        '        k = self._api_key if self._api_key is not None else os.getenv("GEMINI_API_KEY")\n'
        "        self._client = genai.Client(api_key=k)\n",
        "        k = self._api_key\n        j = k\n"
        "        self._client = genai.Client(api_key=j)\n",
    ],
)
def test_the_key_may_reach_the_client_through_or_a_conditional_or_a_local(body):
    assert key_errors(init(body)) == []


@pytest.mark.parametrize(
    "body",
    [
        "        self._client = genai.Client()\n",
        '        self._client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))\n',
        '        k = os.getenv("GEMINI_API_KEY")\n        self._client = genai.Client(api_key=k)\n',
        "        k = self._api_key\n        self._client = genai.Client(api_key=other)\n",
    ],
)
def test_a_rewrite_that_drops_the_key_still_fails(body):
    errors = key_errors(init(body))
    assert errors and "never drop a key value" in errors[0]


def missing(tmp_path: Path, candidate: str) -> list[str]:
    (tmp_path / "app.py").write_text(ORIGINAL)
    findings = scan(tmp_path, "google-genai").findings
    return [
        e
        for e in unrelated_edits(
            ORIGINAL, candidate, findings, frozenset({"google.generativeai", "google"})
        )
        if "is missing" in e
    ]


def test_names_read_through_getattr_and_hasattr_used_as_attributes_are_not_dropped(tmp_path):
    candidate = (
        ORIGINAL.replace("import google.generativeai as genai", "from google import genai")
        .replace('hasattr(response, "candidates") and ', "")
        .replace('getattr(candidate, "finish_reason", None)', "candidate.finish_reason")
    )
    assert missing(tmp_path, candidate) == []


def test_a_rewrite_that_loses_the_safety_branch_is_still_reported(tmp_path):
    candidate = ORIGINAL.replace(
        "import google.generativeai as genai", "from google import genai"
    ).replace(
        "            except ValueError as e:\n"
        '                if hasattr(response, "candidates") and response.candidates:\n'
        "                    candidate = response.candidates[0]\n"
        '                    finish_reason = getattr(candidate, "finish_reason", None)\n'
        "                    if finish_reason == 2:\n"
        '                        raise RuntimeError("blocked by safety filters") from e\n'
        "                raise\n",
        "            except ValueError:\n                text = ''\n",
    )
    errors = missing(tmp_path, candidate)
    assert any("'blocked by safety filters'" in e for e in errors)
    assert any("'candidates'" in e for e in errors)


# --- the report's words for a reverted whitespace-only change ---------------------------------

WHITESPACE_ORIGINAL = (
    "import google.generativeai as genai\n"
    "\n"
    "def test_ask():\n"
    "    first = 1\n"
    "    \n"  # a blank line that holds four spaces
    "    second = 2\n"
)


def test_a_reverted_whitespace_only_line_says_so(tmp_path):
    candidate = WHITESPACE_ORIGINAL.replace("    \n", "\n").replace(
        "import google.generativeai as genai", "from google import genai"
    )
    (tmp_path / "t.py").write_text(WHITESPACE_ORIGINAL)
    findings = scan(tmp_path, "google-genai").findings
    result = revert_text_edits(
        WHITESPACE_ORIGINAL, candidate, findings, frozenset({"google.generativeai", "google"})
    )
    (reverted,) = [r.to_dict() for r in result.reverted if r.line == 5]
    text = revert_text(reverted)
    assert '""' not in text
    assert text == (
        "whitespace changed on a blank line outside the migration, restored to the original"
    )


def test_revert_text_names_the_line_or_a_blank_line():
    base = {"line": 5, "fixed_line": 5, "kind": "changed"}
    assert revert_text({**base, "old": "x = 1", "new": "x = 1"}) == (
        'whitespace changed on the line "x = 1" outside the migration, restored to the original'
    )
    assert revert_text({**base, "old": "x = 1", "new": ""}) == (
        'text changed outside the migration: a blank line restored to "x = 1"'
    )
    assert revert_text({**base, "old": "x = 1", "new": "x = 2"}) == (
        'text changed outside the migration: "x = 2" restored to the original'
    )


# --- the key keeps its source ----------------------------------------------------------------

ENV_ORIGINAL = """\
import os
import google.generativeai as genai


def build():
    api_key = os.environ.get("GOOGLE_API_KEY")
    genai.configure(api_key=api_key)
"""


def env_errors(candidate: str) -> list[str]:
    return MIGRATION.rewrite_checker(Path("."), [])("app.py", ENV_ORIGINAL, candidate)["key"]


def env_candidate(body: str) -> str:
    return "import os\nfrom google import genai\n\n\ndef build():\n" + body


def test_a_changed_key_source_is_named_plainly():
    errors = env_errors(
        env_candidate(
            '    client_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")\n'
            "    return genai.Client(api_key=client_api_key)\n"
        )
    )
    assert len(errors) == 1
    assert 'reads the key from `os.environ.get("GOOGLE_API_KEY")`' in errors[0]
    assert "keep that exactly, don't add GEMINI_API_KEY" in errors[0]


def test_a_fallback_tried_before_the_original_is_rejected():
    errors = env_errors(
        env_candidate(
            '    api_key = os.environ.get("GOOGLE_API_KEY")\n'
            '    return genai.Client(api_key=os.getenv("GEMINI_API_KEY") or api_key)\n'
        )
    )
    assert len(errors) == 1 and "don't add GEMINI_API_KEY" in errors[0]


def test_the_original_key_followed_by_a_fallback_is_accepted():
    assert (
        env_errors(
            env_candidate(
                '    api_key = os.environ.get("GOOGLE_API_KEY")\n'
                '    return genai.Client(api_key=api_key or os.getenv("GEMINI_API_KEY"))\n'
            )
        )
        == []
    )


def test_the_key_read_unchanged_is_accepted():
    assert (
        env_errors(
            env_candidate(
                '    api_key = os.environ.get("GOOGLE_API_KEY")\n'
                "    return genai.Client(api_key=api_key)\n"
            )
        )
        == []
    )


def test_an_attribute_key_with_an_env_fallback_after_it_is_still_accepted():
    body = (
        "        self._client = genai.Client(\n"
        '            api_key=self._api_key or os.getenv("GEMINI_API_KEY")\n        )\n'
    )
    assert key_errors(init(body)) == []


def test_an_env_fallback_before_an_attribute_key_is_rejected():
    body = (
        "        self._client = genai.Client(\n"
        '            api_key=os.getenv("GEMINI_API_KEY") or self._api_key\n        )\n'
    )
    errors = key_errors(init(body))
    assert len(errors) == 1 and "GEMINI_API_KEY" in errors[0] and "first" in errors[0]
