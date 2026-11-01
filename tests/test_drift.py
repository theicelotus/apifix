"""Behavior drift inside migrated lines: a dropped `from` clause, a replaced argument variable."""

from __future__ import annotations

import textwrap

from apifix.engine.migrations import get_migration
from apifix.engine.scanner import scan_text
from apifix.engine.unrelated import unrelated_edits

ORIGINAL = textwrap.dedent(
    """\
    import time

    import openai


    def ask(prompt, retries, wait_time):
        for attempt in range(retries):
            try:
                response = openai.ChatCompletion.create(
                    model="gpt-4", messages=[{"role": "user", "content": prompt}]
                )
                return response["choices"][0]["message"]["content"]
            except openai.error.RateLimitError as e:
                if attempt == retries - 1:
                    raise RuntimeError(
                        "rate limited"
                    ) from e
                time.sleep(wait_time)
    """
)

MIGRATED = textwrap.dedent(
    """\
    import time

    import openai


    def ask(prompt, retries, wait_time):
        client = openai.OpenAI()
        for attempt in range(retries):
            try:
                response = client.chat.completions.create(
                    model="gpt-4", messages=[{"role": "user", "content": prompt}]
                )
                return response.choices[0].message.content
            except openai.RateLimitError as e:
                if attempt == retries - 1:
                    raise RuntimeError(
                        "rate limited"
                    ) from e
                time.sleep(wait_time)
    """
)


def check(candidate: str, original: str = ORIGINAL, renames=None) -> list[str]:
    findings = scan_text("m.py", original, get_migration("openai-v1"))
    return unrelated_edits(original, candidate, findings, frozenset({"openai"}), renames=renames)


def test_faithful_migration_has_no_drift():
    assert check(MIGRATED) == []


def test_dropped_from_clause_is_reported():
    errors = check(MIGRATED.replace("    ) from e", "    )"))
    assert len(errors) == 1
    assert "`raise ... from e` lost its `from e`" in errors[0]
    assert errors[0].startswith("original line 15:")


def test_renamed_cause_is_not_a_dropped_one():
    renamed = MIGRATED.replace("as e:", "as err:").replace(") from e", ") from err")
    assert check(renamed) == []


def test_replaced_argument_variable_is_reported():
    errors = check(MIGRATED.replace("time.sleep(wait_time)", "time.sleep(2**attempt)"))
    drift = [e for e in errors if "the argument `wait_time`" in e]
    assert len(drift) == 1
    assert "`time.sleep(...)` became `2 ** attempt`" in drift[0]
    assert drift[0].startswith("original line 18:")


def test_variable_renamed_throughout_is_fine():
    renamed = MIGRATED.replace("wait_time", "delay")
    assert check(renamed) == []


def test_arguments_of_the_migrated_call_may_change():
    # the migration renames engine= to model=, and moves the value through unchanged
    original = ORIGINAL.replace('model="gpt-4"', "engine=engine").replace(
        "def ask(prompt,", "def ask(engine, prompt,"
    )
    migrated = MIGRATED.replace('model="gpt-4"', "model=engine").replace(
        "def ask(prompt,", "def ask(engine, prompt,"
    )
    assert check(migrated, original, renames={"engine": "model"}) == []


def test_added_calls_do_not_confuse_pairing():
    extra = MIGRATED.replace(
        "time.sleep(wait_time)", "print('retrying')\n                time.sleep(wait_time)"
    )
    assert check(extra) == []


def test_expression_still_built_from_the_variable_is_fine():
    errors = check(MIGRATED.replace("time.sleep(wait_time)", "time.sleep(wait_time * 2)"))
    assert not [e for e in errors if "the argument" in e]


GENAI_ORIGINAL = textwrap.dedent(
    """\
    import google.generativeai as genai
    from google.api_core import exceptions as google_exceptions


    class GeminiProvider:
        def __init__(self, api_key):
            genai.configure(api_key=api_key)

        def generate(self, prompt):
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            try:
                text = response.text
            except ValueError as e:
                raise RuntimeError("Gemini blocked the response") from e
            return text

        def call(self, prompt):
            model = genai.GenerativeModel("gemini-1.5-flash")
            try:
                return model.generate_content(prompt).text
            except google_exceptions.ResourceExhausted as e:
                raise RuntimeError("quota exceeded") from e
    """
)

GENAI_MIGRATED = textwrap.dedent(
    """\
    from google import genai
    from google.genai import errors


    class GeminiProvider:
        def __init__(self, api_key):
            self.client = genai.Client(api_key=api_key)

        def generate(self, prompt):
            response = self.client.models.generate_content(
                model="gemini-1.5-flash", contents=prompt
            )
            text = response.text
            if text is None:
                raise RuntimeError("Gemini blocked the response")
            return text

        def call(self, prompt):
            try:
                return self.client.models.generate_content(
                    model="gemini-1.5-flash", contents=prompt
                ).text
            except errors.ClientError as e:
                raise RuntimeError("quota exceeded") from e
    """
)


def check_genai(candidate: str) -> list[str]:
    findings = scan_text("m.py", GENAI_ORIGINAL, get_migration("google-genai"))
    return unrelated_edits(
        GENAI_ORIGINAL, candidate, findings, frozenset({"google.generativeai", "google.genai"})
    )


def test_from_clause_of_a_removed_except_is_not_owed():
    # the old SDK raised ValueError on a blocked response; the new one returns None
    assert check_genai(GENAI_MIGRATED) == []


def test_from_clause_still_owed_while_a_renamed_library_except_remains():
    errors = check_genai(GENAI_MIGRATED.replace('quota exceeded") from e', 'quota exceeded")'))
    assert len(errors) == 1
    assert "lost its `from e`" in errors[0]
    assert errors[0].startswith("original line 23:")


def test_from_clause_still_owed_while_the_same_except_remains():
    old = (
        "        text = response.text\n"
        "        if text is None:\n"
        '            raise RuntimeError("Gemini blocked the response")\n'
    )
    assert old in GENAI_MIGRATED
    kept = GENAI_MIGRATED.replace(
        old,
        "        try:\n"
        "            text = response.text\n"
        "        except ValueError as e:\n"
        '            raise RuntimeError("Gemini blocked the response")\n',
    )
    errors = check_genai(kept)
    assert len(errors) == 1
    assert errors[0].startswith("original line 15:")
