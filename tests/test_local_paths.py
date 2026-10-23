from __future__ import annotations

from apifix.engine.local_paths import redact_local_paths


def test_home_dir_becomes_tilde():
    value = {"repo": "/home/al/targets/x", "notes": ["in /home/al", "cd /home/al/y; ls"]}
    assert redact_local_paths(value, home="/home/al", user="al") == {
        "repo": "~/targets/x",
        "notes": ["in ~", "cd ~/y; ls"],
    }


def test_only_whole_path_prefixes_are_replaced():
    for text in ("/home/alice/x", "/x/home/al/y", "/home/al.bak/z", "/home/al-2"):
        assert redact_local_paths(text, home="/home/al", user="al") == text


def test_pytest_temp_dir_loses_the_user_name():
    text = '"repo": "/tmp/pytest-of-al/pytest-35/t0/repo", "/tmp/pytest-of-alice/p"'
    assert redact_local_paths(text, home="/home/al", user="al") == (
        '"repo": "/tmp/pytest-of-USER/pytest-35/t0/repo", "/tmp/pytest-of-alice/p"'
    )


def test_keys_tuples_and_other_types():
    value = ({"/home/al/a.py": [1, None, True, 2.5]}, "x")
    assert redact_local_paths(value, home="/home/al/", user="al") == (
        {"~/a.py": [1, None, True, 2.5]},
        "x",
    )


def test_root_home_and_no_user_change_nothing():
    assert redact_local_paths("/etc/x and /root", home="/", user="") == "/etc/x and /root"


def test_defaults_use_the_real_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert redact_local_paths(str(tmp_path / "repo")) == "~/repo"
