import subprocess

from apifix.engine import build_info
from apifix.engine.build_info import apifix_build, build_label


def test_build_reports_version_and_commit_of_this_checkout():
    build = apifix_build()
    assert build["version"] == "0.1.0"
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=build_info._SOURCE_DIR
    ).stdout.strip()
    assert build["commit"] == head
    assert isinstance(build["dirty"], bool)


def test_label_marks_a_dirty_tree():
    clean = {"version": "1.2.3", "commit": "a" * 40, "dirty": False}
    assert build_label(clean) == "1.2.3 (git commit aaaaaaaaaaaa)"
    assert build_label({**clean, "dirty": True}) == "1.2.3 (git commit aaaaaaaaaaaa-dirty)"


def test_outside_a_git_checkout_the_commit_is_unknown(monkeypatch):
    monkeypatch.setattr(build_info, "_git", lambda *a: None)
    build = apifix_build()
    assert build["commit"] is None and build["dirty"] is None
    assert build_label(build) == "0.1.0 (git commit unknown)"


def test_dirty_follows_git_status(monkeypatch):
    out = {"rev-parse": "b" * 40, "status": " M x.py"}
    monkeypatch.setattr(build_info, "_git", lambda *a: out[a[0]])
    assert apifix_build()["dirty"] is True
    out["status"] = ""
    assert apifix_build()["dirty"] is False
