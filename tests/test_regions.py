"""Region edits' pure pieces: finding regions, read-only context, labels, reply parsing, block
validation and splicing (synthetic sources)."""

from __future__ import annotations

import ast
import textwrap

import pytest

from apifix.engine.regions import (
    IMPORTS,
    MIN_LINES_FOR_LENGTH_CHECK,
    NAMES_CHARS,
    OUTLINE_CHARS,
    Region,
    apply_blocks,
    attribute_errors,
    check_block,
    coverage,
    fenced_blocks,
    find_regions,
    is_whole_file,
    labels_nudge,
    map_line,
    names_context,
    ordered_ids,
    outline,
    parse_region_blocks,
    region_instruction,
    region_text,
    regions_diff,
    render_block,
    splice,
    split_lines,
)


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def line_of(text: str, needle: str) -> int:
    hits = [n for n, line in enumerate(text.splitlines(), 1) if needle in line]
    assert len(hits) == 1, needle
    return hits[0]


MODULE = src(
    '''
    """Module docstring."""
    from __future__ import annotations

    import os
    import openai
    try:
        import pandas as pd
    except ImportError:
        pd = None

    MODEL = "gpt-3.5-turbo"
    LIMITS = {
        "a": 1,
        "b": 2,
        "c": 3,
    }


    def helper(x):
        return x


    # comment above ask
    @decorator
    def ask(prompt):
        r = openai.ChatCompletion.create(model=MODEL, messages=prompt)
        return helper(r["choices"][0]["message"]["content"])


    async def ask_async(prompt):
        def inner():
            return openai.ChatCompletion.acreate(model=MODEL)
        return await inner()


    @dataclass
    class Bot(openai.Base):
        retries = 3

        def __init__(self):
            self.key = os.environ["K"]

        @staticmethod
        def build():
            return openai.Completion.create(prompt="x")

        def run(self):
            return self.build() + self.fmt()

        def fmt(self):
            return "x"


    class One: pass


    openai.api_key = os.getenv("KEY")

    if __name__ == "__main__":
        print(openai.Model.list())
    '''
)


def plan_for(text: str, *needles: str, **kw):
    return find_regions(text, [line_of(text, n) for n in needles], **kw)


# --- find_regions -----------------------------------------------------------------------------


def test_import_block_is_the_leading_imports_after_docstring_and_future():
    plan = find_regions(MODULE, [])
    assert plan is not None and plan.regions == ()
    imports = plan.imports
    assert (imports.start, imports.end) == (4, 9)  # import os ... pd = None (the try)
    assert region_text(MODULE, imports).startswith("import os\n")
    assert region_text(MODULE, imports).endswith("pd = None\n")


def test_function_region_includes_decorators_not_comments_above():
    plan = plan_for(MODULE, "openai.ChatCompletion.create")
    (r,) = plan.regions
    assert (r.id, r.kind, r.name) == ("R1", "function", "ask")
    assert r.start == line_of(MODULE, "@decorator")
    assert region_text(MODULE, r).startswith("@decorator\ndef ask(prompt):")
    assert "# comment above ask" not in region_text(MODULE, r)


def test_nested_function_gives_the_enclosing_top_level_function():
    (r,) = plan_for(MODULE, "acreate").regions
    assert (r.kind, r.name) == ("function", "ask_async")


def test_methods_class_statements_and_headers():
    plan = plan_for(MODULE, "openai.Completion.create", "retries = 3", "class Bot(openai.Base)")
    kinds = [(r.kind, r.name) for r in plan.regions]
    assert kinds == [("class_header", "Bot"), ("class_statement", "Bot"), ("method", "Bot.build")]
    header, statement, method = plan.regions
    assert region_text(MODULE, header) == "@dataclass\nclass Bot(openai.Base):\n"
    assert header.body_indent == "    "
    assert method.indent == "    " and region_text(MODULE, method).startswith("    @staticmethod\n")
    assert statement.indent == "    "


def test_one_line_class_is_one_class_region():
    (r,) = plan_for(MODULE, "class One: pass").regions
    assert (r.kind, r.name) == ("class", "One")


def test_module_statements_and_main_block():
    plan = plan_for(MODULE, "openai.api_key", "openai.Model.list")
    assert [(r.kind, r.lines) for r in plan.regions] == [("statement", 1), ("statement", 2)]


def test_findings_in_the_import_block_need_no_region_and_gaps_none():
    plan = plan_for(MODULE, "import openai", "# comment above ask")
    assert plan.regions == ()


def test_a_later_import_is_a_statement_region():
    text = src(
        """
        import os
        X = 1
        from openai.error import RateLimitError
        """
    )
    plan = plan_for(text, "RateLimitError")
    assert (plan.imports.start, plan.imports.end) == (1, 1)
    assert [(r.kind, r.start) for r in plan.regions] == [("statement", 3)]


def test_file_without_imports_has_an_empty_block_after_docstring_and_future():
    text = '"""Doc."""\nfrom __future__ import annotations\n\ndef f():\n    return 1\n'
    plan = find_regions(text, [5])
    assert (plan.imports.start, plan.imports.end, plan.imports.lines) == (3, 2, 0)
    assert region_text(text, plan.imports) == ""


def test_regions_are_numbered_in_source_order_and_deduplicated():
    plan = plan_for(MODULE, "openai.Model.list", "acreate", "openai.ChatCompletion.create")
    assert [(r.id, r.name) for r in plan.regions] == [
        ("R1", "ask"),
        ("R2", "ask_async"),
        ("R3", ""),
    ]
    again = find_regions(MODULE, [line_of(MODULE, "r = openai"), line_of(MODULE, "return helper")])
    assert len(again.regions) == 1


def test_previous_plan_keeps_ids_and_new_regions_continue_numbering():
    first = plan_for(MODULE, "openai.Model.list", "openai.ChatCompletion.create")
    assert first.ids == ["R1", "R2"]
    later = find_regions(
        MODULE,
        [line_of(MODULE, "openai.Model.list"), line_of(MODULE, "openai.ChatCompletion.create")],
        feedback_lines=[line_of(MODULE, "return x")],
        previous=first,
    )
    assert [(r.id, r.name, r.source) for r in later.regions] == [
        ("R3", "helper", "feedback"),
        ("R1", "ask", "finding"),
        ("R2", "", "finding"),
    ]


def test_unparsable_file_has_no_plan():
    assert find_regions("def f(:\n", [1]) is None


def test_coverage_and_holding():
    plan = plan_for(MODULE, "openai.ChatCompletion.create")
    assert 0 < coverage(MODULE, plan) < 0.3
    r = plan.regions[0]
    assert plan.holding(r.start) == r and plan.holding(r.start - 1) is None
    assert plan.nearest(r.end + 2) == r  # in the gap below: the region above
    assert plan.nearest(1) == plan.imports  # above everything: the first region below


def test_split_lines_only_at_newlines():
    assert split_lines("a\x0cb\nc") == ["a\x0cb\n", "c"]
    assert split_lines("a\r\nb\r\n") == ["a\r\n", "b\r\n"]


# --- read-only context ------------------------------------------------------------------------


def test_names_context_shows_module_definitions_the_regions_use():
    plan = plan_for(MODULE, "openai.ChatCompletion.create")
    text = names_context(MODULE, plan)
    assert f"# line {line_of(MODULE, 'MODEL =')}\nMODEL = " in text
    assert "def helper(x):  # body not shown" in text
    assert "return x" not in text  # bodies are never shown
    assert "openai" not in text and "decorator" not in text  # imports / undefined: not shown
    assert "LIMITS" not in text  # not used by the region


def test_long_assignments_are_shown_by_their_first_line():
    text = src(
        """
        import openai
        LIMITS = {
            "a": 1,
            "b": 2,
            "c": 3,
        }
        def f():
            return openai.Completion.create(n=LIMITS)
        """
    )
    shown = names_context(text, plan_for(text, "openai.Completion"))
    assert shown == "# line 2\nLIMITS = {  # 4 more lines not shown"


def test_method_regions_show_class_header_attributes_and_called_methods():
    plan = plan_for(MODULE, "return self.build()")
    text = names_context(MODULE, plan)
    assert "@dataclass\nclass Bot(openai.Base):" in text
    assert "    retries = 3" in text
    assert "    @staticmethod\n    def build():  # body not shown" in text
    assert "def fmt(self):  # body not shown" in text
    assert "__init__" not in text  # not called through self.


def test_names_context_shrinks_to_fit():
    many = "".join(f"def helper_{i}(a, b, c, d, e, f, g, h, i, j):\n    pass\n" for i in range(80))
    calls = " + ".join(f"helper_{i}()" for i in range(80))
    text = f"import openai\n{many}def f():\n    return openai.Completion.create(x={calls})\n"
    shown = names_context(text, plan_for(text, "openai.Completion"))
    assert len(shown) <= NAMES_CHARS + 40
    assert shown.startswith("# line 2: helper_0")


def test_outline_marks_regions_and_is_capped():
    plan = plan_for(MODULE, "openai.ChatCompletion.create", "openai.Completion.create")
    text = outline(MODULE, plan)
    assert "4-9 imports  [IMPORTS]" in text
    assert f"{plan.regions[0].start}-{plan.regions[0].end} def ask  [R1]" in text
    assert "  " + f"{plan.regions[1].start}-{plan.regions[1].end} def build  [R2]" in text
    assert "class Bot" in text and "def helper" in text
    big = "".join(f"def function_number_{i}():\n    pass\n" for i in range(200))
    capped = outline(big, find_regions(big, [2]))
    assert len(capped) <= OUTLINE_CHARS + 20 and capped.endswith("more)")


# --- request rendering ------------------------------------------------------------------------


def test_render_block_labels_regions_and_imports():
    plan = plan_for(MODULE, "openai.ChatCompletion.create")
    r = plan.regions[0]
    shown = render_block(r, region_text(MODULE, r), "```")
    assert shown.startswith(
        f"Region R1: def ask (original lines {r.start}-{r.end})\n```python R1\n"
    )
    assert shown.endswith("\n```")
    imports = render_block(plan.imports, region_text(MODULE, plan.imports), "````")
    assert imports.startswith("Imports (IMPORTS, original lines 4-9; return it only if you change")
    assert "\n````python IMPORTS\nimport os\n" in imports
    empty = Region(IMPORTS, "imports", "", 3, 2, source="imports")
    assert render_block(empty, "", "```").endswith(
        "the file has none yet; return it only if you change it):\n```python IMPORTS\n```"
    )


def test_region_instruction_lists_ids():
    text = region_instruction(["R1", "R2"])
    assert "(R1, R2)" in text and "```python R1" in text and IMPORTS in text


# --- parsing replies --------------------------------------------------------------------------

R1 = "def ask(prompt):\n    return 1"
R2 = "def other():\n    return 2"


@pytest.mark.parametrize(
    "reply",
    [
        f"```python R1\n{R1}\n```\n\n```python R2\n{R2}\n```",
        f"```py r1\n{R1}\n```\n```R2\n{R2}\n```",
        f"### R1\n```python\n{R1}\n```\nRegion R2 (updated):\n```python\n{R2}\n```",
        f"**R1**\n\n```python\n{R1}\n```\n```python R2\n{R2}\n```",
    ],
    ids=["info", "info-case", "heading", "mixed-sources"],
)
def test_labelled_blocks(reply):
    parsed = parse_region_blocks(reply, ["R1", "R2"])
    assert parsed.outcome == "ok" and parsed.labels == "labelled"
    assert parsed.blocks == {"R1": R1, "R2": R2}


def test_imports_block_is_optional_and_accepted():
    reply = f"```python IMPORTS\nimport openai\n```\n```python R1\n{R1}\n```"
    parsed = parse_region_blocks(reply, ["R1"])
    assert parsed.blocks == {IMPORTS: "import openai", "R1": R1}


def test_unlabelled_blocks_by_order_when_the_count_matches():
    parsed = parse_region_blocks(f"```python\n{R1}\n```\n```python\n{R2}\n```", ["R1", "R2"])
    assert parsed.outcome == "ok" and parsed.labels == "by_order"
    assert parsed.blocks == {"R1": R1, "R2": R2}
    with_imports = parse_region_blocks(
        f"```python\nfrom openai import OpenAI\nclient = OpenAI()\n```\n```python\n{R1}\n```",
        ["R1"],
    )
    assert with_imports.blocks == {
        IMPORTS: "from openai import OpenAI\nclient = OpenAI()",
        "R1": R1,
    }


def test_unlabelled_blocks_with_the_wrong_count_are_a_label_problem():
    parsed = parse_region_blocks(f"```python\n{R1}\n```", ["R1", "R2"])
    assert parsed.outcome == "labels" and "no labels" in parsed.problem


def test_label_problems_are_named():
    reply = f"```python R1\n{R1}\n```\n```python R1\n{R1}\n```\n```python R7\n{R2}\n```"
    parsed = parse_region_blocks(reply, ["R1", "R2"])
    assert parsed.outcome == "labels"
    assert parsed.problem == "Missing: R2. Not requested: R7. Returned twice: R1."
    assert labels_nudge(["R1", "R2"], parsed.problem).startswith(
        "Return each region in its own code block labelled with its id: R1, R2 (and IMPORTS"
    )
    mixed = parse_region_blocks(f"```python R1\n{R1}\n```\n```python\n{R2}\n```", ["R1", "R2"])
    assert mixed.outcome == "labels" and mixed.problem == "Label every code block."


def test_a_prose_heading_with_two_labels_does_not_label_the_block():
    parsed = parse_region_blocks(f"R1 and R2 below:\n```python\n{R1}\n```", ["R1", "R2"])
    assert parsed.outcome == "labels"


def test_whole_file_reply_is_recognised():
    parsed = parse_region_blocks(f"```python\n{MODULE}```", ["R1"], original=MODULE)
    assert parsed.outcome == "whole" and parsed.body == MODULE.rstrip("\n")
    assert is_whole_file(MODULE, MODULE)
    assert not is_whole_file(R1, MODULE)


def test_no_block_and_truncated():
    assert parse_region_blocks("no code", ["R1"]).outcome == "no_block"
    assert parse_region_blocks(f"```python R1\n{R1}\n```", ["R1"], truncated=True).outcome == (
        "truncated"
    )


def test_fenced_blocks_records_the_line_before_and_unclosed():
    blocks, unclosed = fenced_blocks("intro\n\n### R1\n\n```python R1\nx\n```\nafter\n```\ny")
    assert [(b.info, b.body, b.before) for b in blocks] == [("python R1", "x", "### R1")]
    assert unclosed


# --- check_block ------------------------------------------------------------------------------


def region(text: str, needle: str) -> Region:
    return plan_for(text, needle).regions[0]


def test_function_block_is_accepted_and_trimmed():
    r = region(MODULE, "openai.ChatCompletion.create")
    block = "\n\n@decorator\ndef ask(prompt):\n    return client.chat.completions.create()\n\n"
    result = check_block(r, region_text(MODULE, r), block)
    assert (
        result.error == "" and result.text.startswith("@decorator\n") and result.text.endswith(")")
    )


def test_dedented_method_is_reindented_except_inside_strings():
    text = src(
        '''
        class Bot:
            def ask(self):
                return openai.Completion.create(prompt="""
        first
          second
        """)
        '''
    )
    r = region(text, "openai.Completion")
    block = 'def ask(self):\n    return client.completions.create(prompt="""\nfirst\n  second\n""")'
    result = check_block(r, region_text(text, r), block)
    assert result.error == ""
    assert result.text == (
        '    def ask(self):\n        return client.completions.create(prompt="""\nfirst\n'
        '  second\n""")'
    )


def test_indented_function_block_is_dedented():
    r = region(MODULE, "openai.ChatCompletion.create")
    block = "    @decorator\n    def ask(prompt):\n        return 1"
    result = check_block(r, region_text(MODULE, r), block)
    assert result.error == "" and result.text == "@decorator\ndef ask(prompt):\n    return 1"


def test_other_indentation_is_an_error():
    r = region(MODULE, "openai.Completion.create")  # method, 4 spaces
    result = check_block(r, region_text(MODULE, r), "  def build():\n    return 1")
    assert result.error == "R1: its indentation differs from the original's; keep it"


def test_method_block_must_stay_inside_the_class():
    r = region(MODULE, "openai.Completion.create")
    result = check_block(r, region_text(MODULE, r), "    def build():\n        return 1\nx = 1")
    assert result.error == "R1: line 3 of your block is outside the class; keep the indentation"


@pytest.mark.parametrize(
    ("needle", "block", "error"),
    [
        (
            "openai.ChatCompletion.create",
            "def helper2():\n    pass\n\ndef ask(prompt):\n    return 1",
            "R1: return exactly def ask, nothing before or after it (got: def helper2, def ask); "
            "put new module-level functions in IMPORTS",
        ),
        (
            "openai.ChatCompletion.create",
            "def ask_v1(prompt):\n    return 1",
            "R1: return exactly def ask, nothing before or after it (got: def ask_v1); "
            "put new module-level functions in IMPORTS",
        ),
        (
            "openai.Completion.create",
            "    def make():\n        return 1",
            "R1: return exactly def build, nothing before or after it (got: def make)",
        ),
        ("class One: pass", "class Two: pass", "R1: return exactly the class One"),
        ("class Bot(openai.Base)", "class Robot(Base):", "R1: return exactly the class Bot"),
    ],
    ids=["extra-def", "renamed", "method-renamed", "class", "header"],
)
def test_shape_errors(needle, block, error):
    r = region(MODULE, needle)
    assert check_block(r, region_text(MODULE, r), block).error == error


def test_async_def_may_become_def_and_header_may_change_bases():
    r = region(MODULE, "acreate")
    assert (
        check_block(r, region_text(MODULE, r), "def ask_async(prompt):\n    return 1").error == ""
    )
    h = region(MODULE, "class Bot(openai.Base)")
    assert check_block(h, region_text(MODULE, h), "@dataclass\nclass Bot:").error == ""


def test_statement_regions_may_become_several_statements_or_none():
    r = region(MODULE, "openai.api_key")
    two = 'from openai import OpenAI\nclient = OpenAI(api_key=os.getenv("KEY"))'
    assert check_block(r, region_text(MODULE, r), two).text == two
    assert check_block(r, region_text(MODULE, r), "").error == ""
    c = region(MODULE, "retries = 3")
    assert check_block(c, region_text(MODULE, c), "").error == ""


def test_empty_def_block_is_an_error():
    r = region(MODULE, "openai.ChatCompletion.create")
    assert "the block is empty" in check_block(r, region_text(MODULE, r), "\n\n").error


def test_imports_block_may_gain_imports_assignments_and_functions():
    plan = find_regions(MODULE, [])
    original = region_text(MODULE, plan.imports)
    block = original + (
        "from functools import lru_cache\nclient = openai.OpenAI()\nTIMEOUT: int = 3\n\n"
        "@lru_cache(maxsize=1)\ndef get_client():\n    return openai.OpenAI()\n"
    )
    assert check_block(plan.imports, original, block).error == ""


@pytest.mark.parametrize(
    ("added", "found"),
    [("class Client:\n    pass", "class Client"), ("load_dotenv()", "expr")],
)
def test_imports_block_rejects_other_statements(added, found):
    plan = find_regions(MODULE, [])
    original = region_text(MODULE, plan.imports)
    error = check_block(plan.imports, original, original + added).error
    assert error.startswith("IMPORTS: line 7 of your block: only imports, module-level")
    assert error.endswith(f"(found {found})")


def test_imports_block_keeps_unusual_original_statements():
    text = "import os\nif TYPE_CHECKING:\n    import typing\n\nx = 1\n"
    plan = find_regions(text, [])
    original = region_text(text, plan.imports)
    assert original == "import os\nif TYPE_CHECKING:\n    import typing\n"
    assert check_block(plan.imports, original, original).error == ""


def test_syntax_errors_name_the_line_of_the_block():
    r = region(MODULE, "openai.Completion.create")
    error = check_block(r, region_text(MODULE, r), "    def build():\n        return (\n").error
    assert error.startswith("R1: SyntaxError: ") and "line 2 of your block" in error


def test_placeholders_and_short_blocks_are_rejected():
    r = region(MODULE, "openai.ChatCompletion.create")
    block = "@decorator\ndef ask(prompt):\n    # ... rest unchanged\n    return 1"
    assert check_block(r, region_text(MODULE, r), block).error == (
        "R1: line 3 is a placeholder instead of code ('# ... rest unchanged'); return the whole "
        "region"
    )
    body = "".join(
        f"    x{i} = openai.Completion.create()\n" for i in range(MIN_LINES_FOR_LENGTH_CHECK)
    )
    long = f"def f():\n{body}"
    lr = find_regions(long, [2]).regions[0]
    assert "fewer than half" in check_block(lr, long, "def f():\n    return 1").error


# --- splicing ---------------------------------------------------------------------------------


def test_splice_replaces_regions_bottom_up_and_maps_lines():
    plan = plan_for(MODULE, "openai.ChatCompletion.create", "openai.Model.list")
    ask, main = plan.regions
    texts = {
        "R1": "@decorator\ndef ask(prompt):\n    r = client.chat.completions.create()\n"
        "    x = 1\n    return r.choices[0].message.content",
        "R2": 'if __name__ == "__main__":\n    print(client.models.list())',
        IMPORTS: region_text(MODULE, plan.imports) + "client = openai.OpenAI()",
    }
    spliced = splice(MODULE, plan, texts)
    ast.parse(spliced.text)
    assert spliced.text.endswith("    print(client.models.list())\n")
    assert "client = openai.OpenAI()\n\nMODEL" in spliced.text
    lines = spliced.text.splitlines()
    n = lines.index("    x = 1") + 1
    assert spliced.locate(n) == ("R1", 4)
    assert spliced.locate(1) == (None, 1)
    assert spliced.locate(lines.index("def helper(x):") + 1) == (
        None,
        line_of(MODULE, "def helper"),
    )
    untouched = splice(MODULE, plan, {})
    assert untouched.text == MODULE


def test_splice_keeps_crlf_and_missing_final_newline():
    text = "import openai\r\n\r\ndef f():\r\n    return openai.Completion.create()"
    plan = find_regions(text, [4])
    out = splice(text, plan, {"R1": "def f():\n    return client.completions.create()"}).text
    assert out == "import openai\r\n\r\ndef f():\r\n    return client.completions.create()"


def test_splice_into_an_empty_import_block():
    text = '"""Doc."""\ndef f():\n    return openai.Completion.create()\n'
    plan = find_regions(text, [3])
    out = splice(text, plan, {IMPORTS: "import openai\n"}).text
    assert out == '"""Doc."""\nimport openai\ndef f():\n    return openai.Completion.create()\n'


def test_apply_blocks_checks_then_splices_over_a_base():
    plan = plan_for(MODULE, "openai.ChatCompletion.create", "openai.Model.list")
    base = {"R2": 'if __name__ == "__main__":\n    print(client.models.list())'}
    result = apply_blocks(MODULE, plan, {"R1": "@decorator\ndef ask(prompt):\n    return 1"}, base)
    assert result.errors == []
    assert "print(client.models.list())" in result.text and "    return 1\n" in result.text
    assert set(result.texts) == {"R1", "R2"} and result.spliced is not None


def test_apply_blocks_reports_block_errors():
    plan = plan_for(MODULE, "openai.ChatCompletion.create")
    result = apply_blocks(MODULE, plan, {"R1": "def nope():\n    pass"})
    assert result.text == "" and result.errors[0].startswith("R1: return exactly def ask")


def test_a_syntax_error_only_splicing_reveals_names_the_region():
    text = "def f():\n    return 1\n"
    plan = find_regions(text, [1])
    broken = Region("R1", "statement", "", 2, 2)  # a hand-made span: a statement in a def
    plan = type(plan)(plan.imports, (broken,))
    result = apply_blocks(text, plan, {"R1": "return 2"})
    assert result.errors == [
        "R1, line 1 of your block: SyntaxError after splicing: "
        "expected an indented block after function definition on line 1"
    ]


# --- attribution and region diffs -------------------------------------------------------------


def two_regions():
    plan = plan_for(MODULE, "openai.ChatCompletion.create", "openai.Model.list")
    texts = {
        "R1": "@decorator\ndef ask(prompt):\n    r = client.chat.completions.create()\n"
        "    return undefined_thing(r)",
        "R2": 'if __name__ == "__main__":\n    print(client.models.list())',
    }
    return plan, texts, splice(MODULE, plan, texts)


def test_errors_are_attributed_by_prefix_original_line_and_target():
    plan, texts, spliced = two_regions()
    ask = plan.regions[0]
    errors = [
        "R2: SyntaxError: invalid syntax (line 1 of your block)",
        f'original line {ask.start + 1}: changed code unrelated to the migration ("x")',
        f'after original line {ask.end + 1}: added code unrelated to the migration ("y")',
        "mock target 'app.client.chat': app.py has no top-level name 'client'",
        "something with no place",
    ]
    texts = {**texts, "R2": texts["R2"] + "\n@patch('app.client.chat')"}
    got = attribute_errors(errors, plan, spliced, spliced.text, texts, fallback=["R1"])
    assert [ids for _, ids in got] == [["R2"], ["R1"], ["R1"], ["R2"], ["R1"]]
    assert [e for e, _ in got] == errors  # these forms are not rewritten


def test_candidate_lines_are_rewritten_to_the_region_block_line():
    plan, texts, spliced = two_regions()
    n = spliced.text.splitlines().index("    return undefined_thing(r)") + 1
    checked = "import json\n" + spliced.text  # an auto-import shifts every line by one
    (got,) = attribute_errors(
        [f"line {n + 1}: 'undefined_thing' is used but never imported or defined"],
        plan,
        spliced,
        checked,
        texts,
        fallback=[],
    )
    assert got == (
        "R1, line 4 of your block: 'undefined_thing' is used but never imported or defined",
        ["R1"],
    )


def test_map_line_through_insertions():
    before, after = "a\nb\nc\n", "a\nNEW\nb\nc\n"
    assert [map_line(before, after, n) for n in (1, 2, 3, 4)] == [1, 1, 2, 3]


def test_regions_diff_uses_file_line_numbers_and_skips_unchanged():
    plan, texts, _ = two_regions()
    unchanged = {"R2": region_text(MODULE, plan.regions[1])}
    assert regions_diff("m.py", MODULE, plan, unchanged, ["R2"]) == ""
    diff = regions_diff("m.py", MODULE, plan, texts, ["R2", "R1"])
    assert diff.index("--- a/m.py R1") < diff.index("--- a/m.py R2")  # file order
    start = plan.regions[0].start
    assert f"@@ -{start + 1}," in diff
    assert len(regions_diff("m.py", MODULE, plan, texts, ["R1"], limit=60)) <= 60 + 25
    assert ordered_ids(plan, ["R2", IMPORTS, "R1", "R2"]) == [IMPORTS, "R1", "R2"]


def test_region_instruction_with_required_imports():
    text = region_instruction([IMPORTS, "R2"])
    assert text.startswith("Return every region (IMPORTS, R2)")
    assert "only if you change it" not in text
    assert region_instruction(["R1"]).startswith("Return the region (R1)")
