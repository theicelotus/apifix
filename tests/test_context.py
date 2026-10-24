"""Compact read-only context for test files' requests (synthetic sources)."""

from __future__ import annotations

import textwrap

from apifix.engine.context import changed_statements, library_uses, reachable_modules

LIB = frozenset({"openai"})


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def reader(files: dict[str, str]):
    return lambda rel: files.get(rel)


FILES = {
    "tests/test_x.py": "from pkg import Bot\nimport helpers.util\n",
    "pkg/__init__.py": "from .bot import Bot\n",
    "pkg/bot.py": "from . import llm\nimport os\n",
    "pkg/llm.py": "import openai\n",
    "helpers/__init__.py": "",
    "helpers/util.py": "from other import thing\n",
    "other.py": "",
    "unrelated.py": "import openai\n",
}


def test_reachable_modules_follow_absolute_relative_and_reexports():
    wanted = {"pkg/bot.py", "pkg/llm.py", "other.py", "unrelated.py"}
    found = reachable_modules("tests/test_x.py", reader(FILES), ("tests/", "", "src/"), wanted)
    assert found == ["pkg/bot.py", "other.py", "pkg/llm.py"]


def test_reachable_modules_under_src_and_unparsable_files():
    files = {"tests/t.py": "import app\n", "src/app.py": "def (\n"}
    assert reachable_modules("tests/t.py", reader(files), ("", "src/"), {"src/app.py"}) == [
        "src/app.py"
    ]


ORIGINAL = src(
    '''
    import os

    import openai


    class Bot:
        """A bot."""

        def __init__(self):
            self.key = os.environ["K"]

        def ask(self, q):
            return openai.ChatCompletion.create(messages=q)

        def other(self):
            return 1


    def helper():
        return 2
    '''
)
FIXED = src(
    '''
    import os

    import openai


    class Bot:
        """A bot."""

        def __init__(self):
            self.key = os.environ["K"]
            self.client = openai.OpenAI(api_key=self.key)

        def ask(self, q):
            response = self.client.chat.completions.create(messages=q)
            return response.choices[0].message.content

        def other(self):
            return 1


    def helper():
        return 2
    '''
)


def test_changed_statements_show_changed_members_with_class_header_and_imports():
    text = changed_statements(ORIGINAL, FIXED, LIB)
    assert text.startswith("# line 3\nimport openai\n# line 6\nclass Bot:\n")
    assert "    def __init__(self):\n" in text and "self.client = openai.OpenAI" in text
    assert "def ask(self, q):" in text
    assert "def other" not in text and "def helper" not in text and "import os" not in text


def test_changed_statements_of_an_unparsable_file_is_the_file():
    assert changed_statements("x = 1\n", "def (\n", LIB) == "def (\n"


def test_library_uses_follow_names_and_self_attributes():
    text = library_uses(FIXED, LIB)
    assert text.split("\n# line ")[0] == "# line 3\nimport openai"
    assert "# line 11 (in Bot.__init__)\nself.client = openai.OpenAI(api_key=self.key)" in text
    assert "# line 14 (in Bot.ask)\nresponse = self.client.chat.completions.create(messages=q)" in (
        text
    )
    assert "(in Bot.ask)\nreturn response.choices[0].message.content" in text
    assert "os.environ" not in text and "return 1" not in text


def test_library_uses_from_imports_module_clients_and_the_cap():
    text = src(
        """
        from openai import OpenAI as Client
        client = Client()
        def ask():
            with open("x") as f:
                return client.completions.create(prompt=f.read())
        """
    )
    shown = library_uses(text, LIB)
    assert "# line 2\nclient = Client()" in shown
    assert "# line 5 (in ask)\nreturn client.completions.create(prompt=f.read())" in shown
    capped = library_uses(text, LIB, limit=60)
    assert len(capped) <= 60 + 25 and capped.endswith("# ... (more not shown)")
    assert library_uses("import os\n", LIB) == ""
