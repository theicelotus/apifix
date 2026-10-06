"""End-to-end verifier runs in real sandboxes: one baseline, three candidate fixes.

Needs Docker and network (installs openai 0.28.1 for the baseline and 1.x for the fixes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apifix.engine.docker import DockerCLI
from apifix.engine.sandbox import Sandbox, SandboxConfig
from apifix.engine.verifier import verify
from tests.integration.tiny_repo import ORIGINAL, make_repo

_usable, _why = DockerCLI().available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _usable, reason=_why or "no docker")]

CONFIG = SandboxConfig(install_timeout_s=600, test_timeout_s=180, memory="1g", cpus=1.0)

GOOD_APP = """\
    import openai

    client = openai.OpenAI(api_key="unused-in-tests")


    def ask(question):
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": question}],
        )
        return response.choices[0].message.content.strip()
    """

GOOD_TESTS = """\
    from types import SimpleNamespace as NS
    from unittest import mock

    from app import ask


    def completion(text):
        return NS(choices=[NS(message=NS(content=text))])


    @mock.patch("app.client.chat.completions.create")
    def test_ask(create):
        create.return_value = completion("hello")
        assert ask("hi") == "hello"


    @mock.patch("app.client.chat.completions.create")
    def test_strips_whitespace(create):
        create.return_value = completion("  hello\\n")
        assert ask("hi") == "hello"
    """

GOOD_FIX = {
    "requirements.txt": "openai>=1.0,<2\npytest\n",
    "app.py": GOOD_APP,
    "tests/test_app.py": GOOD_TESTS,
}


@pytest.fixture(scope="module")
def sandbox() -> Sandbox:
    return Sandbox(CONFIG)


@pytest.fixture(scope="module")
def original(tmp_path_factory) -> Path:
    return make_repo(tmp_path_factory.mktemp("original"), ORIGINAL)


@pytest.fixture(scope="module")
def baseline(sandbox, original):
    result = sandbox.run(original)
    assert result.status == "passed", (result.reason, result.test and result.test.stdout)
    assert result.counts.passed == 2
    return result


def check(sandbox, original, baseline, tmp_path, fixed_files):
    fixed = make_repo(tmp_path / "fixed", {**ORIGINAL, **fixed_files})
    return verify(original, fixed, "openai-v1", sandbox=sandbox, baseline=baseline)


def test_good_fix_is_verified(sandbox, original, baseline, tmp_path):
    v = check(sandbox, original, baseline, tmp_path, GOOD_FIX)
    assert v.verdict == "verified", v.report_markdown()
    assert v.fixed.counts.passed == 2
    assert v.coverage.available and v.coverage.ratio == 1.0
    assert v.remaining_findings == []
    assert "## Verification: Verified" in v.report_markdown()


def test_fix_that_breaks_a_test_is_rejected(sandbox, original, baseline, tmp_path):
    buggy = GOOD_APP.replace(".content.strip()", ".content")
    v = check(sandbox, original, baseline, tmp_path, {**GOOD_FIX, "app.py": buggy})
    assert v.verdict == "rejected"
    (regression,) = [r for r in v.reasons if r.code == "regression"]
    assert regression.tests == ("tests.test_app::test_strips_whitespace",)
    assert v.rerun is not None  # the new failure was rerun before being blamed


def test_fix_that_deletes_a_test_is_rejected(sandbox, original, baseline, tmp_path):
    # Keep everything up to the second test, i.e. delete test_strips_whitespace.
    fewer = GOOD_TESTS[: GOOD_TESTS.rindex('    @mock.patch("app.client')]
    v = check(sandbox, original, baseline, tmp_path, {**GOOD_FIX, "tests/test_app.py": fewer})
    assert v.verdict == "rejected"
    codes = {r.code: r for r in v.reasons}
    assert codes["deleted_test"].tests == ("tests.test_app::test_strips_whitespace",)
    assert codes["tampering.test_removed"].tests == ("test_strips_whitespace",)
