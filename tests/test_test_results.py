from __future__ import annotations

import json

import pytest

from apifix.engine.test_results import (
    ReportError,
    TestCaseResult,
    TestCounts,
    parse_coverage_json,
    parse_junit,
    parse_unittest_output,
)

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="2" tests="6">
  <testcase classname="tests.test_chat" name="test_answer" time="0.01"/>
  <testcase classname="tests.test_chat" name="test_retry[3]" time="0.01">
    <failure message="assert 1 == 2">trace</failure>
  </testcase>
  <testcase classname="tests.test_chat" name="test_slow"><skipped message="slow"/></testcase>
  <testcase classname="tests.test_chat" name="test_xfail">
    <skipped type="pytest.xfail" message="known"/>
  </testcase>
  <testcase classname="tests.test_db" name="test_teardown"/>
  <testcase classname="tests.test_db" name="test_teardown"><error message="teardown"/></testcase>
  <testcase classname="" name="tests/test_broken.py"><error message="collect"/></testcase>
</testsuite></testsuites>
"""


def test_parse_junit():
    counts, cases = parse_junit(JUNIT)
    assert counts == TestCounts(total=6, passed=1, failed=1, skipped=2, errors=2)
    outcomes = {c.id: c.outcome for c in cases}
    assert outcomes["tests.test_chat::test_retry[3]"] == "failed"
    # a pass plus a teardown error for the same test collapses to the worse outcome
    assert outcomes["tests.test_db::test_teardown"] == "error"
    assert outcomes["tests/test_broken.py"] == "error"
    messages = {c.id: c.message for c in cases}
    assert messages["tests.test_chat::test_retry[3]"] == "assert 1 == 2\ntrace"
    assert messages["tests.test_chat::test_answer"] == ""


def test_parse_junit_empty_suite():
    counts, cases = parse_junit('<testsuites><testsuite tests="0"/></testsuites>')
    assert counts == TestCounts() and cases == []


@pytest.mark.parametrize(
    "xml",
    [
        "<testsuites><testsuite>",
        '<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites>&a;</testsuites>',
    ],
)
def test_parse_junit_rejects_bad_reports(xml):
    with pytest.raises(ReportError):
        parse_junit(xml)


UNITTEST_311 = """\
test_add (tests.test_math.MathTest.test_add) ... ok
test_div (tests.test_math.MathTest.test_div) ... FAIL
test_io (tests.test_math.MathTest.test_io) ... ERROR
test_net (tests.test_math.MathTest.test_net) ... skipped 'no network'
test_doc (tests.test_math.MathTest.test_doc)
Adds two numbers. ... ok
test_known (tests.test_math.MathTest.test_known) ... expected failure

======================================================================
FAIL: test_div (tests.test_math.MathTest.test_div)
----------------------------------------------------------------------
Ran 6 tests in 0.003s

FAILED (failures=1, errors=1, skipped=1, expected failures=1)
"""


def test_parse_unittest_311_format():
    counts, cases = parse_unittest_output(UNITTEST_311)
    assert counts == TestCounts(total=6, passed=2, failed=1, skipped=2, errors=1)
    assert [c.id for c in cases] == [
        "tests.test_math.MathTest.test_add",
        "tests.test_math.MathTest.test_div",
        "tests.test_math.MathTest.test_io",
        "tests.test_math.MathTest.test_net",
        "tests.test_math.MathTest.test_doc",
        "tests.test_math.MathTest.test_known",
    ]
    assert cases[3].outcome == "skipped"


def test_parse_unittest_legacy_format_and_ok_summary():
    text = (
        "test_a (test_mod.T) ... ok\n"
        "test_b (test_mod.T) ... skipped 'x'\n\n"
        "----\nRan 2 tests in 0.001s\n\nOK (skipped=1)\n"
    )
    counts, cases = parse_unittest_output(text)
    assert counts == TestCounts(total=2, passed=1, skipped=1)
    assert cases[0] == TestCaseResult("test_mod.T.test_a", "passed")


def test_parse_unittest_failure_messages():
    text = """\
test_ok (m.T.test_ok) ... ok
test_net (m.T.test_net) ... ERROR

======================================================================
ERROR: test_net (m.T.test_net)
----------------------------------------------------------------------
Traceback (most recent call last):
openai.APIConnectionError: Connection error.

----------------------------------------------------------------------
Ran 2 tests in 0.01s

FAILED (errors=1)
"""
    _, cases = parse_unittest_output(text)
    by_id = {c.id: c for c in cases}
    assert by_id["m.T.test_ok"].message == ""
    assert "APIConnectionError" in by_id["m.T.test_net"].message


def test_parse_coverage_json():
    text = json.dumps(
        {
            "files": {
                "/work/src/app.py": {"executed_lines": [1, 2, 5], "missing_lines": [6]},
                "src/rel.py": {"executed_lines": [], "missing_lines": [1]},
            }
        }
    )
    cov = parse_coverage_json(text)
    assert cov["src/app.py"].executed == {1, 2, 5}
    assert cov["src/app.py"].executable == {1, 2, 5, 6}
    assert cov["src/rel.py"].missing == {1}


@pytest.mark.parametrize("text", ["not json", "[]", '{"files": 3}'])
def test_parse_coverage_json_rejects_bad_reports(text):
    with pytest.raises(ReportError):
        parse_coverage_json(text)


def test_parse_unittest_without_summary_counts_lines():
    counts, _ = parse_unittest_output("test_a (m.T.test_a) ... ok\ntest_b (m.T.test_b) ... FAIL\n")
    assert counts == TestCounts(total=2, passed=1, failed=1)


def test_parse_unittest_no_tests():
    counts, cases = parse_unittest_output("\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n")
    assert counts == TestCounts() and cases == []


# Synthetic -v output: a test logs to stderr between "... " and its status, and tests with
# docstrings get the docstring's first line after their name (also in ERROR/FAIL blocks).
UNITTEST_INTERLEAVED = """\
test_ping (testserver.ServerTest.test_ping)
Ping answers. ... ok
test_reply (testserver.ServerTest.test_reply)
Reply comes back. ... 2026-01-01 10:00:00,000 - INFO - replied
ok
test_upload (testserver.ServerTest.test_upload)
Upload reads a data file. ... ERROR
test_quiet (testserver.ServerTest.test_quiet) ... FAIL

======================================================================
ERROR: test_upload (testserver.ServerTest.test_upload)
Upload reads a data file.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "/work/svc/testserver.py", line 9, in test_upload
FileNotFoundError: [Errno 2] No such file or directory: 'data/sample.bin'

======================================================================
FAIL: test_quiet (testserver.ServerTest.test_quiet)
----------------------------------------------------------------------
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 4 tests in 0.010s

FAILED (failures=1, errors=1)
"""


def test_parse_unittest_log_line_between_dots_and_status():
    counts, cases = parse_unittest_output(UNITTEST_INTERLEAVED)
    assert counts == TestCounts(total=4, passed=2, failed=1, errors=1)
    outcomes = {c.id: c.outcome for c in cases}
    assert outcomes == {
        "testserver.ServerTest.test_ping": "passed",
        "testserver.ServerTest.test_reply": "passed",
        "testserver.ServerTest.test_upload": "error",
        "testserver.ServerTest.test_quiet": "failed",
    }


def test_parse_unittest_messages_for_tests_with_docstrings():
    _, cases = parse_unittest_output(UNITTEST_INTERLEAVED)
    messages = {c.id: c.message for c in cases}
    assert "FileNotFoundError" in messages["testserver.ServerTest.test_upload"]
    assert "Upload reads" not in messages["testserver.ServerTest.test_upload"]
    assert messages["testserver.ServerTest.test_quiet"].startswith("AssertionError: 1 != 2")


# --- missing modules --------------------------------------------------------------------------


def test_missing_module_is_read_from_the_whole_traceback_before_clipping():
    trace = "\n".join(
        f"/work/src/app/mod{i}.py:{i}: in &lt;module&gt;\n    import x" for i in range(40)
    )
    xml = f"""<testsuites><testsuite>
      <testcase classname="" name="tests.test_app"><error message="collection failure">\
ImportError while importing test module '/work/tests/test_app.py'.
Traceback:
{trace}
E   ModuleNotFoundError: No module named 'dotenv'</error></testcase>
      <testcase classname="tests.test_b" name="test_ok"/>
      <testcase classname="tests.test_b" name="test_bad">
        <failure message="assert 1 == 2"/></testcase>
    </testsuite></testsuites>"""
    _, cases = parse_junit(xml)
    by_id = {c.id: c for c in cases}
    broken = by_id["tests.test_app"]
    assert "dotenv" not in broken.message  # clipped to the first 500 characters
    assert broken.missing_module == "dotenv"
    assert by_id["tests.test_b::test_bad"].missing_module == ""
    assert by_id["tests.test_b::test_ok"].missing_module == ""


def test_missing_module_is_the_last_one_named_and_keeps_dotted_names():
    xml = """<testsuites><testsuite><testcase classname="t" name="test_x"><error message="e">\
ModuleNotFoundError: No module named 'yaml'
During handling of the above exception, another exception occurred:
ModuleNotFoundError: No module named "google.protobuf"</error></testcase>
    </testsuite></testsuites>"""
    _, (case,) = parse_junit(xml)
    assert case.missing_module == "google.protobuf"


def test_unittest_import_errors_have_their_missing_module():
    text = """\
test_app (unittest.loader._FailedTest.test_app) ... ERROR

======================================================================
ERROR: test_app (unittest.loader._FailedTest.test_app)
----------------------------------------------------------------------
ImportError: Failed to import test module: test_app
Traceback (most recent call last):
  File "/work/test_app.py", line 1, in <module>
    import spotipy
ModuleNotFoundError: No module named 'spotipy'


----------------------------------------------------------------------
Ran 1 test in 0.001s

FAILED (errors=1)
"""
    _, (case,) = parse_unittest_output(text)
    assert case.outcome == "error" and case.missing_module == "spotipy"
