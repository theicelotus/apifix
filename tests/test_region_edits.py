"""Region edits in whole-file mode, end to end with the scripted fake LLM and fake sandbox of
test_agent: the form chosen per file, labelled replies spliced back, check retries per region,
batches, the switch after a refused whole file, trajectory and report."""

from __future__ import annotations

import pytest

from apifix import cli
from tests.test_agent import (
    APP_NEW,
    TESTS_OLD,
    FakeLLM,
    JudgingSandbox,
    block,
    done,
    files_of,
    too_large,
    wf,
)
from tests.test_verifier import APP_OLD, ORIGINAL, TESTS_NEW, write_tree

# A test file over the region threshold: the two tests with findings, plus many without.
EXTRAS = "".join(
    f'\n\ndef test_extra_{i}():\n    text = "{"abc " * 80}"\n    assert len(text) == 320\n'
    for i in range(25)
)
TESTS_BIG = TESTS_OLD + EXTRAS

IMPORTS_NEW = """\
from types import SimpleNamespace as NS
from unittest import mock

from app import ask


def completion(text):
    return NS(choices=[NS(message=NS(content=text))])
"""
R1_NEW = """\
@mock.patch("app.client.chat.completions.create")
def test_ask(create):
    create.return_value = completion(" hi ")
    assert ask("q") == "hi"
"""
R2_NEW = """\
@mock.patch("app.client.chat.completions.create")
def test_strips(create):
    create.return_value = completion("x\\n")
    assert ask("q") == "x"
"""


def regions(*pairs: tuple[str, str], labelled: bool = True) -> object:
    """A reply with one fenced block per (label, text)."""
    parts = [
        f"```python {label}\n{text}```" if labelled else f"```python\n{text}```"
        for label, text in pairs
    ]
    return done("Here are the regions:\n\n" + "\n\n".join(parts))


GOOD_REGIONS = regions(("IMPORTS", IMPORTS_NEW), ("R1", R1_NEW), ("R2", R2_NEW))


@pytest.fixture
def repo(tmp_path):
    return write_tree(tmp_path / "repo", ORIGINAL)


@pytest.fixture
def big(repo):
    (repo / "tests" / "test_app.py").write_text(TESTS_BIG)
    return repo


def request_texts(llm):
    return [r[0].text for r in llm.requests]


def test_a_large_test_file_is_sent_as_regions_and_spliced_back(big):
    llm, sandbox = FakeLLM([block(APP_NEW), GOOD_REGIONS]), JudgingSandbox()
    result = wf(big, llm, sandbox)
    assert result.outcome == "verified"
    app_request, test_request = request_texts(llm)
    assert app_request.startswith("Migrate this file: app.py")
    assert "Edit only these regions of tests/test_app.py" in test_request
    assert "Region R1: def test_ask (original lines 6-9)\n```python R1\n" in test_request
    assert "Region R2: def test_strips (original lines 12-15)" in test_request
    assert "Imports (IMPORTS, original lines 1-3; return it only if you change it):" in test_request
    assert "test_extra_7" in test_request  # in the outline only
    assert "assert len(text) == 320" not in test_request  # bodies outside regions: not sent
    assert "Files already updated for this migration" in test_request  # app.py as context
    written = (big / "tests" / "test_app.py").read_text()
    assert written == TESTS_BIG  # the repo itself is never modified
    fixed = result.diff
    assert '+@mock.patch("app.client.chat.completions.create")' in fixed
    assert "+def completion(text):" in fixed
    assert "test_extra" not in fixed

    app_entry, entry = files_of(result)
    assert (app_entry["form"], app_entry["form_reason"]) == ("whole", "small")
    assert (entry["form"], entry["form_reason"], entry["accepted"]) == ("regions", "size", True)
    assert [(r["id"], r["name"], r["returned"], r["changed"]) for r in entry["regions"]] == [
        ("R1", "test_ask", True, True),
        ("R2", "test_strips", True, True),
    ]
    assert entry["replies"][0]["labels"] == "labelled"
    old = TESTS_OLD.split("\n\n\n")
    assert entry["prompt_parts"]["regions"] == len(old[1]) + 1 + len(old[2])
    assert len(entry["batches"]) == 1 and entry["batches"][0]["ids"] == ["R1", "R2"]
    assert entry["bytes_sent"] < len(TESTS_BIG) // 3
    assert (
        f"- Region edits: `tests/test_app.py` ({len(TESTS_BIG):,} bytes, over 8,000): "
        "R1 test_ask (lines 6-9), R2 test_strips (lines 12-15)" in result.report_markdown
    )


def test_region_reply_budget_follows_the_regions(big):
    llm = FakeLLM([block(APP_NEW), GOOD_REGIONS])
    wf(big, llm)
    shown = TESTS_OLD.split("\n\n\n")
    tokens = -(-(len(shown[0]) + 1 + len(shown[1]) + 1 + len(shown[2])) // 3)
    assert abs(llm.max_tokens_seen[1] - (2 * tokens + 2048)) <= 4
    assert llm.max_tokens_seen[1] < llm.max_tokens_seen[0] + 2048


def test_unlabelled_blocks_in_order_are_accepted(big):
    reply = regions(("", IMPORTS_NEW), ("", R1_NEW), ("", R2_NEW), labelled=False)
    result = wf(big, FakeLLM([block(APP_NEW), reply]))
    assert result.outcome == "verified"
    assert files_of(result)[1]["replies"][0]["labels"] == "by_order"


def test_missing_labels_get_a_nudge_in_the_same_conversation(big):
    llm = FakeLLM([block(APP_NEW), regions(("IMPORTS", IMPORTS_NEW), ("R1", R1_NEW)), GOOD_REGIONS])
    result = wf(big, llm)
    assert result.outcome == "verified"
    nudge = result.trajectory["attempts"][0]["nudges"][0]
    assert nudge["kind"] == "code_block" and nudge["outcome"] == "labels"
    assert nudge["message"] == (
        "Return each region in its own code block labelled with its id: R1, R2 (and IMPORTS if "
        "you change it). Missing: R2."
    )
    assert llm.requests[2][-1].text == nudge["message"]  # appended to the conversation


def test_a_check_error_resends_only_the_region_it_concerns(big):
    bad_r2 = R2_NEW.replace("completion(", "make_completion(")
    first = regions(("IMPORTS", IMPORTS_NEW), ("R1", R1_NEW), ("R2", bad_r2))
    retry = regions(("R2", R2_NEW))
    llm = FakeLLM([block(APP_NEW), first, retry])
    result = wf(big, llm)
    assert result.outcome == "verified"
    nudge = next(
        n for n in result.trajectory["attempts"][0]["nudges"] if n["kind"] == "check_retry"
    )
    assert nudge["regions_resent"] == ["R2"]
    assert nudge["error"] == (
        "R2, line 3 of your block: 'make_completion' is used but never imported or defined"
    )
    request = llm.requests[2][0].text
    assert "Region R2: def test_strips" in request and "Region R1:" not in request
    assert "Your previous version of R2 was rejected: R2, line 3 of your block:" in request
    assert "b/tests/test_app.py R2" in request  # its rejected version as a diff
    assert "kept as they are (read-only):" in request and "b/tests/test_app.py R1" in request
    assert "def completion(text):" in request  # IMPORTS as last returned
    assert "Return the region (R2)" in request
    assert '+    create.return_value = completion("x\\n")' in result.diff


def test_a_region_error_after_the_retry_fails_the_file(big):
    bad = R2_NEW.replace("def test_strips", "def test_strips_renamed")
    llm = FakeLLM([block(APP_NEW), regions(("R1", R1_NEW), ("R2", bad)), regions(("R2", bad))])
    result = wf(big, llm, max_retries=0)
    entry = files_of(result)[1]
    assert not entry["accepted"]
    assert entry["error"].startswith("R2: return exactly def test_strips")
    assert result.outcome == "aborted"


def test_whole_file_returned_to_a_region_request_is_accepted(big):
    whole = f"{IMPORTS_NEW}\n\n{R1_NEW}\n\n{R2_NEW}{EXTRAS}"
    llm = FakeLLM([block(APP_NEW), block(whole)])
    result = wf(big, llm)
    assert result.outcome == "verified"
    entry = files_of(result)[1]
    assert entry["replies"][0]["parse"] == "whole" and entry["accepted"]


def test_later_attempt_resends_every_region_with_its_previous_version(big):
    broken = APP_NEW + "# BREAK\n"
    llm = FakeLLM([block(broken), GOOD_REGIONS, block(APP_NEW), GOOD_REGIONS])
    result = wf(big, llm)
    assert result.outcome == "verified"
    request = llm.requests[3][0].text
    assert request.startswith("The verifier did not accept attempt 1")
    assert "Region R1:" in request and "Region R2:" in request
    assert "Your previous version of R1, R2 did not pass verification (see above)." in request


def test_forced_regions_for_a_small_file_and_forced_whole_for_a_large_one(repo, big):
    app_regions = regions(
        ("R1", APP_NEW.split("\n\n\n")[1]),
        ("IMPORTS", 'import openai\n\nclient = openai.OpenAI(api_key="test")\n'),
    )
    llm = FakeLLM([app_regions, GOOD_REGIONS])
    result = wf(big, llm, file_edits="regions")
    assert [f["form_reason"] for f in files_of(result)] == ["forced", "forced"]
    assert result.outcome == "verified"
    llm = FakeLLM([block(APP_NEW), block(TESTS_BIG.replace("x", "x"))])
    result = wf(big, llm, file_edits="whole", max_retries=0)
    assert [f["form"] for f in files_of(result)] == ["whole", "whole"]


def test_threshold_and_coverage_rules(big):
    llm = FakeLLM([block(APP_NEW), block(TESTS_BIG)])
    result = wf(big, llm, region_threshold_bytes=len(TESTS_BIG) + 1, max_retries=0)
    assert files_of(result)[1]["form_reason"] == "small"
    dense = TESTS_OLD + "\n\nX = 1\n"  # the regions are most of the file
    (big / "tests" / "test_app.py").write_text(dense)
    llm = FakeLLM([block(APP_NEW), block(TESTS_OLD)])
    result = wf(big, llm, region_threshold_bytes=100, max_retries=0)
    assert (files_of(result)[1]["form"], files_of(result)[1]["form_reason"]) == (
        "whole",
        "coverage",
    )


def test_files_over_the_size_limit_go_as_regions_or_are_skipped(big):
    llm = FakeLLM([block(APP_NEW), GOOD_REGIONS])
    result = wf(big, llm, whole_file_max_bytes=4_000)
    entry = files_of(result)[1]
    assert (entry["form"], entry["form_reason"]) == ("regions", "size")
    assert result.outcome == "verified"
    llm = FakeLLM([block(APP_NEW)])
    result = wf(big, llm, whole_file_max_bytes=60, max_retries=0)
    skipped = result.trajectory["job"]["skipped_files"]
    assert skipped[-1]["path"] == "tests/test_app.py"
    assert "and so is region R1 (def test_ask (original lines 6-9)" in skipped[-1]["reason"]


def test_regions_are_batched_to_fit_the_size_limit(big):
    batch1 = regions(("IMPORTS", IMPORTS_NEW), ("R1", R1_NEW))
    batch2 = regions(("R2", R2_NEW))
    llm = FakeLLM([block(APP_NEW), batch1, batch2])
    imports = len(TESTS_OLD.split("\n\n\n")[0]) + 1
    limit = imports + len(TESTS_OLD.split("\n\n\n")[1]) + 30
    result = wf(big, llm, whole_file_max_bytes=limit)  # app.py still fits whole
    entry = files_of(result)[1]
    assert [b["ids"] for b in entry["batches"]] == [["R1"], ["R2"]]
    assert result.outcome == "verified"
    second = llm.requests[2][0].text
    assert "Region R2:" in second and "Region R1:" not in second
    assert "def completion(text):" in second  # IMPORTS as the first batch left it
    assert "b/tests/test_app.py R1" in second  # the first batch's changes, read-only


def test_a_refused_whole_file_is_sent_as_regions(repo):
    padded = TESTS_OLD + "".join(f"\n\ndef test_x{i}():\n    assert True\n" for i in range(30))
    (repo / "tests" / "test_app.py").write_text(padded)  # under the threshold: whole first
    llm = FakeLLM([block(APP_NEW), too_large(8_000, 30_000), GOOD_REGIONS])
    result = wf(repo, llm)
    assert result.outcome == "verified"
    refused, entry = files_of(result)[1:]
    assert refused["form"] == "whole" and refused["switched_to_regions"]
    assert refused["error"].startswith("provider_error: HTTP 413")
    assert (entry["form"], entry["form_reason"], entry["accepted"]) == ("regions", "refused", True)
    assert "Edit only these regions of tests/test_app.py" in llm.requests[2][0].text


def test_a_refused_region_request_stops_the_job(repo):
    llm = FakeLLM([too_large(1000, 5000), too_large(1000, 5000)])
    result = wf(repo, llm)
    assert result.outcome == "aborted"
    assert result.reason.endswith("(request for app.py)")
    whole, regions_entry = files_of(result)
    assert whole["switched_to_regions"] and regions_entry["form_reason"] == "refused"
    assert "Edit only these regions of app.py" in llm.requests[1][0].text


def test_a_refused_whole_file_still_aborts_when_forced_whole(repo):
    llm = FakeLLM([block(APP_NEW), too_large(8_000, 30_000)])
    result = wf(repo, llm, file_edits="whole")
    assert result.outcome == "aborted" and "request for tests/test_app.py" in result.reason


def test_unknown_file_edits_value_is_refused():
    from apifix.engine.agent import AgentConfig

    with pytest.raises(ValueError, match="unknown file edits"):
        AgentConfig(file_edits="parts")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--file-edits", "regions"], "--file-edits only applies to --edit-mode whole-file"),
        (
            ["--region-threshold", "100"],
            "--region-threshold only applies to --edit-mode whole-file",
        ),
        (
            ["--edit-mode", "whole-file", "--region-threshold", "0"],
            "--region-threshold must be positive",
        ),
    ],
)
def test_cli_region_flags_are_checked(repo, capsys, args, message):
    code = cli.main(["fix", str(repo), "--migration", "openai-v1", *args])
    assert code == 2
    assert message in capsys.readouterr().err


def test_apifix_adds_a_missing_import_into_the_import_block(big):
    imports = IMPORTS_NEW.replace("from types import SimpleNamespace as NS\n", "").replace(
        "NS(", "SimpleNamespace("
    )
    llm = FakeLLM([block(APP_NEW), regions(("IMPORTS", imports), ("R1", R1_NEW), ("R2", R2_NEW))])
    result = wf(big, llm)
    assert result.outcome == "verified"
    assert "+from types import SimpleNamespace" in result.diff
    assert "- apifix added: from types import SimpleNamespace (tests/test_app.py, attempt 1)" in (
        result.report_markdown
    )


# --- stage 3: known limits, compact context, regions from verifier output ---------------------


def lowered(limit: int, max_tokens: int) -> Exception:
    """A refusal that fewer reply tokens fix (the request is then sent again with
    max_tokens 100 below `max_tokens`, or with 244 tokens for a small limit)."""
    margin = max(256, -(-3 * limit // 100))
    prompt = max(limit - margin - max_tokens + 100, 100)
    return too_large(limit, max_tokens + prompt)


def test_a_known_limit_trims_later_requests(repo):
    app_budget = 2 * -(-len(APP_OLD) // 3) + 2048
    llm = FakeLLM([lowered(9_000, app_budget), block(APP_NEW), block(TESTS_NEW)])
    result = wf(repo, llm)
    assert result.outcome == "verified"
    job = result.trajectory["job"]
    assert job["limits"]["per_minute"] == 9_000 and job["limits"]["chars_per_token"] > 0
    app_entry, tests_entry = files_of(result)
    assert "guide_sections" not in app_entry  # sent before the limit was known
    assert "tests" in tests_entry["guide_sections"]
    assert tests_entry["context_forms"] == {"app.py": "changed"}
    assert "--- app.py (changed statements only)" in llm.requests[2][0].text
    assert "- Token limit known (9,000)" in result.report_markdown


def test_non_test_files_get_no_tests_guide_part_once_a_limit_is_known(repo):
    broken = APP_NEW + "# BREAK\n"
    app_budget = 2 * -(-len(APP_OLD) // 3) + 2048
    script = [lowered(9_000, app_budget), block(broken), block(TESTS_NEW), block(APP_NEW)]
    llm = FakeLLM([*script, block(TESTS_NEW)])
    result = wf(repo, llm)
    assert result.outcome == "verified"
    app_again = files_of(result, 1)[0]
    assert app_again["path"] == "app.py" and "tests" not in app_again["guide_sections"]


def test_a_whole_file_over_a_known_limit_is_sent_as_regions(big):
    app_budget = 2 * -(-len(APP_OLD) // 3) + 2048
    llm = FakeLLM([lowered(3_000, app_budget), block(APP_NEW), GOOD_REGIONS])
    result = wf(big, llm, region_threshold_bytes=100_000)  # not by size
    assert result.outcome == "verified"
    entry = files_of(result)[1]
    assert (entry["form"], entry["form_reason"]) == ("regions", "limit")
    assert [f["form"] for f in files_of(result)] == ["whole", "regions"]


def test_regions_are_batched_to_fit_a_known_limit(big):
    app_budget = 2 * -(-len(APP_OLD) // 3) + 2048
    batch1 = regions(("IMPORTS", IMPORTS_NEW), ("R1", R1_NEW))
    llm = FakeLLM([lowered(600, app_budget), block(APP_NEW), batch1, regions(("R2", R2_NEW))])
    result = wf(big, llm)
    entry = files_of(result)[1]
    assert [b["ids"] for b in entry["batches"]] == [["R1"], ["R2"]]
    assert entry["context_files"] == []  # nothing fits beside the regions
    assert result.outcome == "verified"


def extra_judge(tree):
    """test_extra_3 (no findings) fails on the fix until its region is changed."""
    from tests.test_agent import judge
    from tests.test_verifier import T_ASK, T_STRIP, run

    tests = (tree / "tests" / "test_app.py").read_text()
    extra = "tests.test_app::test_extra_3"
    cov = judge(tree).coverage
    if '"openai.ChatCompletion.create"' in tests or 'openai.OpenAI(api_key="x")' in tests:
        return run((T_ASK, "passed"), (T_STRIP, "passed"), (extra, "passed"), coverage=cov)
    message = "tests/test_app.py:40: in test_extra_3\n    assert len(text) == 320\nAssertionError"
    return run((T_ASK, "passed"), (T_STRIP, "passed"), (extra, "failed", message), coverage=cov)


def test_a_later_attempt_adds_regions_the_verifier_points_at(big):
    line = next(n for n, t in enumerate(TESTS_BIG.splitlines(), 1) if "def test_extra_3" in t)
    extra_new = (  # an edit that uses the library: not unrelated (apifix adds the import)
        'def test_extra_3():\n    client = openai.OpenAI(api_key="x")\n'
        f'    text = "{"abc " * 80}"\n    assert len(text) == 320\n'
    )
    second = regions(("R1", R1_NEW), ("R2", R2_NEW), ("R3", extra_new), ("IMPORTS", IMPORTS_NEW))
    llm = FakeLLM([block(APP_NEW), GOOD_REGIONS, block(APP_NEW), second])
    result = wf(big, llm, JudgingSandbox(judge=extra_judge))
    assert result.outcome == "verified"
    entry = files_of(result, 1)[1]
    assert entry["regions"][-1] == {
        "id": "R3",
        "kind": "function",
        "name": "test_extra_3",
        "lines": [line, line + 2],
        "source": "feedback",
        "bytes": len(TESTS_BIG.splitlines(keepends=True)[line - 1])
        + len(TESTS_BIG.splitlines(keepends=True)[line])
        + len(TESTS_BIG.splitlines(keepends=True)[line + 1]),
        "returned": True,
        "changed": True,
    }
    request = llm.requests[3][0].text
    assert f"Region R3: def test_extra_3 (original lines {line}-{line + 2})" in request
    assert "+import openai" in result.diff and '+    client = openai.OpenAI(api_key="x")' in (
        result.diff
    )
