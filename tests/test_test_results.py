from __future__ import annotations

import pytest

from apifix.engine.test_results import (
    ReportError,
    TestCaseResult,
    TestCounts,
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
    assert TestCaseResult("tests.test_chat::test_retry[3]", "failed") in cases
    # a pass plus a teardown error for the same test collapses to the worse outcome
    assert TestCaseResult("tests.test_db::test_teardown", "error") in cases
    assert TestCaseResult("tests/test_broken.py", "error") in cases


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


def test_parse_unittest_without_summary_counts_lines():
    counts, _ = parse_unittest_output("test_a (m.T.test_a) ... ok\ntest_b (m.T.test_b) ... FAIL\n")
    assert counts == TestCounts(total=2, passed=1, failed=1)


def test_parse_unittest_no_tests():
    counts, cases = parse_unittest_output("\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n")
    assert counts == TestCounts() and cases == []
