"""The unrelated-edit check on the split_config fixture (synthetic, modelled on two patterns
from a real run: a word changed inside a prompt string, and edits away from the migration)."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from apifix.engine import unrelated
from apifix.engine.migrations import get_migration
from apifix.engine.scanner import scan
from apifix.engine.unrelated import Revert, revert_text_edits, unrelated_edits
from apifix.engine.whole_file import error_key

FIXTURE = Path(__file__).parent / "fixtures" / "split_config"
MIGRATION = get_migration("openai-v1")
FINDINGS = scan(FIXTURE, "openai-v1").findings
LIBRARY = frozenset({"openai"})
ASSISTANT = "voicebot/assistant.py"
CLI = "voicebot/cli.py"
TESTS = "tests/test_assistant.py"


def original(path: str) -> str:
    return (FIXTURE / path).read_text()


def edit(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


# A hand-written good migration: keep the configuration and use the module-level client.
GOOD_ASSISTANT = edit(
    edit(
        original(ASSISTANT),
        "response = openai.ChatCompletion.create(",
        "response = openai.chat.completions.create(",
    ),
    'response["choices"][0]["message"]["content"]',
    "response.choices[0].message.content",
)
GOOD_CLI = edit(original(CLI), "openai.api_base = base_url", "openai.base_url = base_url")
GOOD_TESTS = (
    original(TESTS)
    .replace('"openai.ChatCompletion.create"', '"openai.chat.completions.create"')
    .replace(
        "import unittest\nfrom unittest import mock\n",
        "import unittest\nfrom types import SimpleNamespace\nfrom unittest import mock\n"
        "\nimport openai\n",
    )
    .replace(
        "class AssistantTest(unittest.TestCase):\n",
        "class AssistantTest(unittest.TestCase):\n"
        "    def setUp(self):\n"
        '        openai.api_key = "test"  # the module-level client needs a key; none is sent\n\n',
    )
)
GOOD_TESTS = edit(
    GOOD_TESTS,
    """        create.return_value = {
            "model": "gpt-3.5-turbo",
            "choices": [{"message": {"content": " Hello \\n  there "}}],
        }
""",
    """        create.return_value = SimpleNamespace(
            model="gpt-3.5-turbo",
            choices=[SimpleNamespace(message=SimpleNamespace(content=" Hello \\n  there "))],
        )
""",
)


def own(path: str) -> list:
    return [f for f in FINDINGS if f.file == path]


def check(path: str, candidate: str):
    """(the revert result, every error: reverts that did not parse, then code and strings)."""
    r = revert_text_edits(original(path), candidate, own(path), LIBRARY)
    return r, r.errors + unrelated_edits(original(path), r.text, own(path), LIBRARY)


def test_the_good_migration_passes_untouched():
    for path, good in ((ASSISTANT, GOOD_ASSISTANT), (CLI, GOOD_CLI), (TESTS, GOOD_TESTS)):
        r, errors = check(path, good)
        assert (r.text, r.reverted, errors) == (good, [], []), path


# --- reverted ---------------------------------------------------------------------------------


def test_a_word_inserted_in_a_prompt_string_is_reverted_with_both_line_numbers():
    candidate = edit(GOOD_ASSISTANT, "import openai\n", "import os\n\nimport openai\n")
    candidate = edit(candidate, "rate how  well", "rate how such well")
    r, errors = check(ASSISTANT, candidate)
    assert errors == []
    assert r.text == edit(GOOD_ASSISTANT, "import openai\n", "import os\n\nimport openai\n")
    assert r.reverted == [
        Revert(
            line=10,
            fixed_line=12,  # two lines were added above it
            kind="changed",
            old="Then rate how  well the answer fits the...",
            new="Then rate how such well the answer fits the...",
        )
    ]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("voice assistant.   \n", "voice assistant.\n"),  # trailing spaces inside the string
        ('"""Collapse whitespace so the speech engine reads it smoothly."""', '"""Tidy text."""'),
        ("# Answers longer than this are cut off by the speech engine.", "# Max answer length."),
        ("# Answers longer than this are cut off by the speech engine.\n", ""),
        ("\ndef _clean(", "\n# --- helpers ---\n\ndef _clean("),  # comment lines inserted
        ("MAX_WORDS = 80\n", "MAX_WORDS   =   80\n"),  # whitespace away from the migration
    ],
)
def test_text_only_edits_away_from_the_migration_are_reverted(old, new):
    r, errors = check(ASSISTANT, edit(GOOD_ASSISTANT, old, new))
    assert (r.text, errors) == (GOOD_ASSISTANT, [])
    assert len(r.reverted) == 1


def test_revert_kinds_and_excerpts():
    r, _ = check(
        ASSISTANT, edit(GOOD_ASSISTANT, "\ndef _clean(", "\n# --- helpers ---\ndef _clean(")
    )
    (added,) = r.reverted
    assert (added.kind, added.line, added.old, added.new) == ("added", 12, "", "# --- helpers ---")
    gone = "# Answers longer than this are cut off by the speech engine.\n"
    r, _ = check(ASSISTANT, edit(GOOD_ASSISTANT, gone, ""))
    (removed,) = r.reverted
    assert (removed.kind, removed.line, removed.new) == ("removed", 5, "")
    assert removed.old == gone.strip()


# --- docstrings: text, however the diff splits them --------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "kind", "excerpt"),
    [
        # added to a method far from the findings, several lines
        (
            "    def describe(self) -> str:\n",
            '    def describe(self) -> str:\n        """\n        Say which voice this is.\n'
            '\n        Used in logs.\n        """\n',
            "added",
            "Say which voice this is.",
        ),
        # removed (the function's body is then its return)
        ('    """The first choice of a stored completion."""\n', "", "removed", ""),
        ('"""Answers spoken questions with a chat model."""\n\n', "", "removed", ""),
        # rewritten across lines
        (
            '    """Wraps one chat model."""',
            '    """Wraps one chat model.\n\n    Replies are short.\n    """',
            "changed",
            '..."""Wraps one chat model.',
        ),
        # a comment turned into a docstring
        (
            "        # The voice is fixed per instance.\n",
            '        """The voice is fixed per instance."""\n',
            "changed",
            '"""The voice is fixed per instance."""',
        ),
    ],
)
def test_docstring_edits_away_from_the_migration_are_reverted(old, new, kind, excerpt):
    r, errors = check(ASSISTANT, edit(GOOD_ASSISTANT, old, new))
    assert (r.text, errors) == (GOOD_ASSISTANT, [])
    (revert,) = r.reverted
    assert revert.kind == kind
    assert (revert.new if kind != "removed" else revert.old).startswith(excerpt)


SHOUT = """import openai


def ask(q):
    r = openai.ChatCompletion.create(model="m", messages=[{"role": "user", "content": q}])
    return r["choices"][0]["message"]["content"]


def shout(text):
    # Split on whitespace.

    words = text.split()
    return " ".join(w.upper() for w in words)
"""


def test_a_docstring_the_diff_splits_is_reverted_piece_by_piece():
    candidate = SHOUT.replace(
        "    # Split on whitespace.\n\n    words",
        '    """Upper-case every word.\n\n    Used for headings.\n    """\n    words',
    )
    findings = MIGRATION.check_python("app.py", ast.parse(SHOUT), SHOUT)
    # the docstring's blank line lines up with the original's, so two hunks hold its pieces
    assert len(unrelated.hunks(SHOUT, candidate)) == 2
    r = revert_text_edits(SHOUT, candidate, findings, LIBRARY)
    assert (r.text, r.errors) == (SHOUT, [])
    assert [(x.kind, x.new) for x in r.reverted] == [
        ("changed", '"""Upper-case every word.'),
        ("added", "Used for headings."),
    ]


def test_a_docstring_next_to_a_related_change_is_reverted_and_the_change_kept():
    """The pattern of a real run: the model documented a migrated helper, and an unchanged
    line split the docstring from the body change, so it was a hunk of its own."""
    from tests.test_streaming import FIXTURE, GENERATE, migrated

    findings = [f for f in scan(FIXTURE, "openai-v1").findings if f.file == GENERATE]
    original = (FIXTURE / GENERATE).read_text()
    good = migrated()
    documented = good.replace(
        "    for chunk in completion:\n",
        '    """\n    Yield the text of each streamed chunk.\n    """\n'
        "    for chunk in completion:\n",
    )
    r = revert_text_edits(original, documented, findings, LIBRARY)
    assert (r.text, r.errors) == (good, [])
    assert [(x.line, x.kind, x.new) for x in r.reverted] == [
        (14, "added", "Yield the text of each streamed chunk.")
    ]
    assert unrelated_edits(original, r.text, findings, LIBRARY) == []


def test_code_beside_a_docstring_is_still_judged_as_code():
    # one hunk: a docstring change and an unrelated code change; the message shows the code
    both = edit(
        GOOD_ASSISTANT,
        '    """Collapse whitespace so the speech engine reads it smoothly."""\n'
        '    return " ".join(text.split()[:MAX_WORDS])',
        '    """Collapse whitespace."""\n    return " ".join(text.split()[: MAX_WORDS - 1])',
    )
    assert check(ASSISTANT, both)[1] == [
        'original line 15: changed code unrelated to the migration ("return " ".join('
        'text.split()[:MAX_WORDS])"); keep it exactly as in the original'
    ]
    # a string statement after the first one is no docstring
    note = edit(
        GOOD_ASSISTANT,
        "        self.voice = voice\n",
        '        self.voice = voice\n        "the voice name"\n',
    )
    assert check(ASSISTANT, note)[1] == [
        'after original line 27: added code unrelated to the migration (""the voice name""); '
        "remove it, make only the changes the migration needs"
    ]


# --- kept -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # client created before the call (uses the library's name), the call rewritten
        (
            "        response = openai.chat.completions.create(",
            "        client = openai.OpenAI()\n        response = client.chat.completions.create(",
        ),
        ("import openai\n", "import os\n\nimport openai\nfrom openai import OpenAI\n"),
        ("import openai\n", ""),  # an import removed
        # a comment reworded within 3 lines of a finding
        ("# Ask the model, then tidy the answer for speech.", "# Call the chat API (1.x)."),
        # a client parameter threaded through a signature, far from the findings (the
        # migrated call uses it)
        (
            '    def __init__(self, voice: str = "calm") -> None:\n        self.voice = voice\n'
            "\n    def reply(self, question: str) -> str:\n"
            "        # Ask the model, then tidy the answer for speech.\n"
            "        response = openai.chat.completions.create(",
            '    def __init__(self, voice: str = "calm", client=None) -> None:\n'
            "        self.voice = voice\n        self.client = client\n"
            "\n    def reply(self, question: str) -> str:\n"
            "        # Ask the model, then tidy the answer for speech.\n"
            "        response = self.client.chat.completions.create(",
        ),
        # a get_client() helper inserted at module level
        (
            "\nclass Assistant:",
            "\ndef get_client():\n    return openai.OpenAI()\n\n\nclass Assistant:",
        ),
        # a whitespace reflow of the migrated call
        (
            '            model="gpt-3.5-turbo",\n            temperature=0.2,\n',
            '            model="gpt-3.5-turbo", temperature=0.2,\n',
        ),
    ],
)
def test_changes_the_migration_needs_are_kept(old, new):
    candidate = edit(GOOD_ASSISTANT, old, new)
    r, errors = check(ASSISTANT, candidate)
    assert (r.text, r.reverted, errors) == (candidate, [], [])


def test_response_access_in_another_function_needs_a_link_to_the_library():
    # first_choice() is never called with a response, so its `data` could be anything (another
    # SDK's dict, stored JSON): the `.choices` attribute alone does not make the edit related
    alone = edit(GOOD_ASSISTANT, '    return data["choices"][0]\n', "    return data.choices[0]\n")
    assert check(ASSISTANT, alone)[1] == [
        'original line 20: changed code unrelated to the migration ("return data["choices"][0]"); '
        "keep it exactly as in the original"
    ]
    # called with the response, its parameter is bound to the library
    linked = edit(
        alone,
        "return _clean(response.choices[0].message.content)",
        "return _clean(first_choice(response).message.content)",
    )
    r, errors = check(ASSISTANT, linked)
    assert (r.text, r.reverted, errors) == (linked, [], [])


def test_mock_targets_response_keys_and_renamed_mocks_are_kept():
    candidate = edit(
        GOOD_TESTS,
        "    def test_constructing_makes_no_call(self, create):\n"
        '        Assistant(voice="bright")\n'
        "        create.assert_not_called()\n",
        "    def test_constructing_makes_no_call(self, mock_create):\n"
        '        Assistant(voice="bright")\n'
        "        mock_create.assert_not_called()\n",
    )
    r, errors = check(TESTS, candidate)
    assert (r.text, r.reverted, errors) == (candidate, [], [])


def test_a_renamed_mock_used_far_from_the_finding_is_kept():
    old = textwrap.dedent(
        '''\
        from unittest import mock

        import pytest


        @pytest.fixture
        def mock_api():
            """Mock the chat API."""
            with mock.patch("openai.ChatCompletion.create") as mocked_create:
                # Configure the mock to return a fixed response.
                mocked_create.return_value = {"choices": [{"message": {"content": "ok"}}]}
                prepared = True
                assert prepared
                checked = True
                assert checked
                counted = 1
                assert counted
                yield mocked_create
        '''
    )
    new = (
        old.replace(
            '"openai.ChatCompletion.create") as mocked_create', '"openai.OpenAI") as MockOpenAI'
        )
        .replace(
            "                mocked_create.return_value = "
            '{"choices": [{"message": {"content": "ok"}}]}',
            "                client = MockOpenAI.return_value\n"
            "                client.chat.completions.create.return_value = SimpleNamespace(\n"
            '                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]\n'
            "                )",
        )
        .replace("yield mocked_create", "yield MockOpenAI")
        .replace("import pytest\n", "import pytest\nfrom types import SimpleNamespace\n")
    )
    findings = MIGRATION.check_python("tests/test_fx.py", ast.parse(old), old)
    r = revert_text_edits(old, new, findings, LIBRARY)
    assert (r.text, r.reverted) == (new, [])
    assert unrelated_edits(old, new, findings, LIBRARY) == []
    # an edit there that touches no migration name is unrelated
    stray = new.replace("assert checked", "assert not checked")
    assert unrelated_edits(old, stray, findings, LIBRARY) == [
        'original line 15: changed code unrelated to the migration ("assert checked"); '
        "keep it exactly as in the original"
    ]


# --- another SDK with the same names (sdk_lookalike fixture) ----------------------------------

LOOKALIKE = Path(__file__).parent / "fixtures" / "sdk_lookalike"
LOOKALIKE_FINDINGS = scan(LOOKALIKE, "openai-v1").findings
CONFTEST = (LOOKALIKE / "conftest.py").read_text()
# the openai mock rewritten for 1.x, as a model did in a real run
GOOD_CONFTEST = edit(
    edit(
        CONFTEST,
        "from unittest.mock import MagicMock, patch\n",
        "from types import SimpleNamespace\nfrom unittest.mock import MagicMock, patch\n",
    ),
    """    with patch("openai.ChatCompletion.create") as mock_create:
        mock_create.return_value = {"choices": [{"message": {"content": canned_reply}}]}
        yield mock_create
""",
    """    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=canned_reply))]
    )
    with patch("src.storyteller.llm.OpenAI", return_value=mock_client):
        yield mock_client
""",
)
# ... and the other SDK's mock rewritten too: its local canned_reply is renamed, so content=
# now refers to the module-level string instead of the mock
LOOKALIKE_CONFTEST = edit(
    edit(
        GOOD_CONFTEST,
        """    canned_reply = MagicMock()
    canned_reply.choices = [MagicMock(message=MagicMock(content=canned_reply))]
""",
        '''    """Patch the Acme client to return a 1.x-style response object."""
    acme_reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=canned_reply))]
    )
''',
    ),
    "acme_client.chat.completions.create.return_value = canned_reply",
    "acme_client.chat.completions.create.return_value = acme_reply",
)


def lookalike_check(path: str, old: str, new: str) -> list[str]:
    findings = [f for f in LOOKALIKE_FINDINGS if f.file == path]
    r = revert_text_edits(old, new, findings, LIBRARY)
    assert r.reverted == []
    return r.errors + unrelated_edits(old, r.text, findings, LIBRARY)


def test_the_other_sdks_mock_is_rejected_and_the_openai_mock_kept():
    assert lookalike_check("conftest.py", CONFTEST, GOOD_CONFTEST) == []
    assert lookalike_check("conftest.py", CONFTEST, LOOKALIKE_CONFTEST) == [
        'original line 22: changed code unrelated to the migration ("canned_reply = '
        'MagicMock()"); keep it exactly as in the original',
        "original line 26: changed code unrelated to the migration "
        '("acme_client.chat.completions.create.return_value = canned_reply"); keep it exactly '
        "as in the original",
    ]


def test_the_app_code_next_to_the_other_sdk_is_kept():
    path = "src/storyteller/llm.py"
    old = (LOOKALIKE / path).read_text()
    new = edit(
        edit(old, "import openai\n", "from openai import OpenAI\n"),
        "        response = openai.ChatCompletion.create(",
        "        client = OpenAI()\n        response = client.chat.completions.create(",
    )
    new = edit(
        new,
        '\n        return response["choices"][0]["message"]["content"]',
        "        # 1.x responses are objects\n        return response.choices[0].message.content",
    )
    assert lookalike_check(path, old, new) == []
    # the other SDK's call, edited with the same names, is not
    acme = edit(
        new,
        "            messages=messages,\n        )\n        return reply",
        "            messages=list(messages),\n        )\n        return reply",
    )
    assert lookalike_check(path, old, acme) == [
        'original line 24: changed code unrelated to the migration ("messages=messages,"); '
        "keep it exactly as in the original"
    ]


BOUND = """\
import acmechat
import openai


def ask(question):
    response = openai.ChatCompletion.create(model="m", messages=[question])
    first = 1
    second = 2
    third = 3
    record(response)
    return first + second + third


def other(question):
    response = acmechat.chat.completions.create(model="a", messages=[question])
    first = 1
    second = 2
    third = 3
    record(response)
    return first + second + third


def get_client():
    return openai.OpenAI()


def get_acme():
    return acmechat.Client()


def ping_openai():
    client = get_client()
    client.ping()


def ping_acme():
    client = get_acme()
    client.ping()


class Bot:
    def __init__(self):
        self.client = openai.OpenAI()
        self.acme = acmechat.Client()

    def close(self):
        self.client.close()
        self.acme.close()


def record(item):
    print(item)
"""
BOUND_MIGRATED = edit(
    BOUND, "response = openai.ChatCompletion.create(", "response = openai.chat.completions.create("
)
RECORD = "    record(response)\n    return first + second + third\n\n\ndef "
RECORD_DUMP = "    record(response.model_dump())\n    return first + second + third\n\n\ndef "


@pytest.mark.parametrize(
    ("old", "new", "line"),
    [
        # the response, used further down the same function
        (RECORD + "other(", RECORD_DUMP + "other(", None),
        # a variable of the same name in another function
        (RECORD + "get_client(", RECORD_DUMP + "get_client(", 19),
        # the other SDK's call, with the same attribute names as the migrated one
        (
            'acmechat.chat.completions.create(model="a", messages=[question])',
            'acmechat.chat.completions.create(model="a", messages=[question, question])',
            15,
        ),
        # a client from a function returning one, and from one returning the other SDK's
        (
            "client = get_client()\n    client.ping()\n",
            "client = get_client()\n    client.ping(1)\n",
            None,
        ),
        (
            "client = get_acme()\n    client.ping()\n",
            "client = get_acme()\n    client.ping(1)\n",
            38,
        ),
        # an instance attribute set from the library, and one that is not
        ("self.client.close()", "self.client.close(timeout=1)", None),
        ("self.acme.close()", "self.acme.close(timeout=1)", 48),
    ],
)
def test_openai_bound_names_relate_a_far_hunk_and_lookalikes_do_not(old, new, line):
    findings = MIGRATION.check_python("bot.py", ast.parse(BOUND), BOUND)
    errors = unrelated_edits(BOUND, edit(BOUND_MIGRATED, old, new), findings, LIBRARY)
    if line is None:
        assert errors == []
    else:
        assert len(errors) == 1, errors
        assert errors[0].startswith(f"original line {line}: changed code unrelated"), errors


PATCHED = """\
from unittest import mock


@mock.patch("acmechat.Client")
@mock.patch("openai.ChatCompletion.create")
def test_reply(create, acme):
    run()
    run()
    run()
    create.assert_called_once()
    acme.assert_called_once()
"""


@pytest.mark.parametrize(
    ("old", "new", "related"),
    [
        ("create.assert_called_once()", "create.assert_called_once_with()", True),
        ("acme.assert_called_once()", "acme.assert_called_once_with()", False),
    ],
)
def test_a_patch_decorator_binds_its_test_parameter(old, new, related):
    findings = MIGRATION.check_python("tests/test_x.py", ast.parse(PATCHED), PATCHED)
    candidate = edit(
        PATCHED.replace('"openai.ChatCompletion.create"', '"openai.chat.completions.create"'),
        old,
        new,
    )
    assert (unrelated_edits(PATCHED, candidate, findings, LIBRARY) == []) is related


# --- rejected ---------------------------------------------------------------------------------


def test_removed_main_block_is_rejected():
    main = '\n\nif __name__ == "__main__":\n    unittest.main()\n'
    _, errors = check(TESTS, edit(GOOD_TESTS, main, "\n"))
    assert errors == [
        "original line 27: removed code unrelated to the migration "
        '("if __name__ == "__main__":"); keep it exactly as in the original'
    ]


def test_unrelated_refactor_and_inserted_helper_are_rejected():
    refactor = edit(
        GOOD_ASSISTANT,
        '    return " ".join(text.split()[:MAX_WORDS])',
        "    words = text.split()\n    return ' '.join(words[:MAX_WORDS])",
    )
    _, errors = check(ASSISTANT, refactor)
    assert errors == [
        """original line 15: changed code unrelated to the migration ("return " ".join(\
text.split()[:MAX_WORDS])"); keep it exactly as in the original"""
    ]
    helper = edit(
        GOOD_ASSISTANT,
        "\nclass Assistant:",
        '\ndef _cache(text):\n    with open("answers.txt", "a") as f:\n'
        "        f.write(text)\n\n\nclass Assistant:",
    )
    _, errors = check(ASSISTANT, helper)
    assert errors == [
        'after original line 22: added code unrelated to the migration ("def _cache(text):"); '
        "remove it, make only the changes the migration needs"
    ]


MISSING = "is missing; keep string literals exactly as in the original unless they name the old API"


def test_strings_changed_with_migrated_code_are_errors():
    model = edit(GOOD_ASSISTANT, 'model="gpt-3.5-turbo"', 'model="gpt-4o-mini"')
    assert check(ASSISTANT, model)[1] == [f"original line 32: the string 'gpt-3.5-turbo' {MISSING}"]
    content = edit(GOOD_TESTS, 'content=" Hello \\n  there "', 'content="Hello there"')
    assert check(TESTS, content)[1] == [
        f"original line 14: the string ' Hello \\n  there ' {MISSING}"
    ]
    # a mock value the test never reads, dropped while the dict became an object
    unused = edit(GOOD_TESTS, '            model="gpt-3.5-turbo",\n', "")
    assert check(TESTS, unused)[1] == [f"original line 13: the string 'gpt-3.5-turbo' {MISSING}"]


def test_a_changed_string_near_a_finding_is_an_error_and_far_away_is_reverted():
    near = edit(
        GOOD_ASSISTANT,
        "        # The voice is fixed per instance.\n"
        '        return f"assistant with a {self.voice} voice"',
        '        return "assistant: " + self.voice',
    )
    assert check(ASSISTANT, near)[1] == [
        f"original line 43: the string ' voice' {MISSING}",
        f"original line 43: the string 'assistant with a ' {MISSING}",
    ]
    far = edit(GOOD_ASSISTANT, '    """Wraps one chat model."""', '    """A chat model."""')
    r, errors = check(ASSISTANT, far)
    assert (r.text, errors) == (GOOD_ASSISTANT, [])


def test_a_string_inside_a_rejected_hunk_gets_one_message():
    candidate = edit(
        GOOD_ASSISTANT,
        '    return " ".join(text.split()[:MAX_WORDS])',
        '    return "-".join(text.split())',
    )
    assert check(ASSISTANT, candidate)[1] == [
        """original line 15: changed code unrelated to the migration ("return " ".join(\
text.split()[:MAX_WORDS])"); keep it exactly as in the original"""
    ]


# --- mechanics --------------------------------------------------------------------------------


def test_a_revert_that_does_not_parse_becomes_an_error(monkeypatch):
    monkeypatch.setattr(unrelated, "_parses", lambda text: False)
    candidate = edit(GOOD_ASSISTANT, "rate how  well", "rate how such well")
    r, errors = check(ASSISTANT, candidate)
    assert (r.text, r.reverted) == (candidate, [])
    assert errors == [  # one message: the string rule leaves the hunk to it
        """original line 10: changed text unrelated to the migration ("Then rate how  well the \
answer fits the question, from 1 to 5.\"\"\""); keep it exactly as in the original"""
    ]


def test_files_that_do_not_parse_are_left_alone():
    r = revert_text_edits("x = (\n", "x = 1\n", [], LIBRARY)
    assert (r.text, r.reverted, r.errors) == ("x = 1\n", [], [])
    assert unrelated_edits("x = 1\n", "x = (\n", [], LIBRARY) == []


def test_crlf_files_keep_their_line_endings():
    old = original(ASSISTANT).replace("\n", "\r\n")
    good = GOOD_ASSISTANT.replace("\n", "\r\n")
    r = revert_text_edits(
        old, edit(good, "rate how  well", "rate how such well"), own(ASSISTANT), LIBRARY
    )
    assert r.text == good


def test_error_key_strips_the_new_line_prefixes():
    assert error_key("original line 54: changed code unrelated (x)") == "changed code unrelated (x)"
    assert error_key("after original line 21: added code") == "added code"
    assert error_key("before original line 1: added code") == "added code"
    assert error_key("line 3: undefined name") == "undefined name"


# --- llmflows run 1: mixed response access and attribute mock data -------------------------------

MIXED = FIXTURE.parent / "mixed_access"


def _mixed_check(path: str, old: str, new: str, *, new_rules: bool = True) -> list[str]:
    """check_candidate's errors for one edit of a mixed_access file; `new_rules=False` drops the
    findings the scanner did not have before (the run's situation)."""
    from apifix.engine.whole_file import check_candidate

    findings = [f for f in scan(MIXED, "openai-v1").findings if f.file == path]
    if not new_rules:
        findings = [
            f
            for f in findings
            if not f.suggestion.startswith(("0.x responses allowed", "1.x code reads responses"))
        ]
    original = (MIXED / path).read_text()
    assert old in original
    candidate = original.replace(old, new)
    r = check_candidate(
        path, original, candidate, required=False, library_modules=LIBRARY, findings=findings
    )
    return r.errors


def test_mixed_access_rewrite_is_related_now():
    args = ("flowkit/llms/completion.py", 'choices[0]["text"]', "choices[0].text")
    (error,) = _mixed_check(*args, new_rules=False)
    assert error.startswith("original line 19: changed code unrelated to the migration")
    assert _mixed_check(*args) == []


def test_attribute_mock_data_conversion_is_related_now():
    args = (
        "tests/test_completion.py",
        "        model_outputs = MagicMock()\n"
        '        model_outputs.choices = [{"text": "test_text"}]\n',
        "        model_outputs = SimpleNamespace(\n"
        '            choices=[SimpleNamespace(text="test_text")]\n'
        "        )\n",
    )
    errors = _mixed_check(*args, new_rules=False)
    assert errors and errors[0].startswith("original line 22: changed code unrelated")
    assert [e for e in _mixed_check(*args) if "unrelated" in e] == []
