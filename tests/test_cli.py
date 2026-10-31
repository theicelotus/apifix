from __future__ import annotations

import json
from pathlib import Path

import pytest

from apifix import cli
from apifix.cli import EXIT_CLEAN, EXIT_FINDINGS, EXIT_USAGE, main
from tests.conftest import FIXTURES


def test_text_output(capsys):
    assert main(["scan", str(FIXTURES / "chatbot.py"), "--migration", "openai-v1"]) == EXIT_FINDINGS
    out = capsys.readouterr().out
    assert "chatbot.py:20:20: api_call [certain]" in out
    assert "    old: openai.ChatCompletion.create" in out
    assert out.rstrip().endswith("6 finding(s) in 1 file(s); 1 file(s) scanned, 0 parse error(s)")


def test_json_output(capsys):
    assert main(["scan", str(FIXTURES), "--migration", "openai-v1", "--json"]) == EXIT_FINDINGS
    data = json.loads(capsys.readouterr().out)
    assert data["migration"] == "openai-v1"
    assert data["files_scanned"] == 9
    assert data["parse_errors"][0]["file"] == "broken_syntax.py"
    first = data["findings"][0]
    assert set(first) == {
        "file",
        "line",
        "col",
        "end_line",
        "end_col",
        "category",
        "old_pattern",
        "suggestion",
        "confidence",
    }


def test_clean_exit(capsys):
    assert main(["scan", str(FIXTURES / "already_v1.py"), "--migration", "openai-v1"]) == EXIT_CLEAN


def test_unknown_migration(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["scan", ".", "--migration", "nope"])
    assert exc.value.code == 2


def test_missing_path(capsys, tmp_path):
    assert main(["scan", str(tmp_path / "x"), "--migration", "openai-v1"]) == EXIT_USAGE
    assert "no such file" in capsys.readouterr().err


def test_github_url_not_supported_yet(capsys):
    assert main(["scan", "https://github.com/a/b", "--migration", "openai-v1"]) == EXIT_USAGE


def test_write_outputs_writes_each_reply_to_replies_with_paths_redacted(tmp_path):
    home = str(Path.home())
    replies = {
        "replies/attempt-1-response-0.txt": f"edited {home}/proj/app.py",
        "replies/attempt-2-response-0.txt": "plain",
    }
    out = tmp_path / "run"
    out.mkdir()
    trajectory = {"job": {}}
    cli._write_outputs(out, "diff", "report", {"verdict": "ok"}, trajectory, replies)
    assert (out / "replies/attempt-1-response-0.txt").read_text() == "edited ~/proj/app.py"
    assert (out / "replies/attempt-2-response-0.txt").read_text() == "plain"
    assert sorted(p.name for p in out.iterdir()) == [
        "fix.diff",
        "replies",
        "report.md",
        "trajectory.json",
        "verification.json",
    ]


def test_write_outputs_without_replies_makes_no_replies_folder(tmp_path):
    cli._write_outputs(tmp_path, "d", "r", None, {"job": {}})
    assert not (tmp_path / "replies").exists()
