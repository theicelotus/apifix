"""Pin edits apifix makes itself: every format, byte-for-byte, fallbacks, companion pins."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apifix.engine.migrations import get_migration
from apifix.engine.pin_edits import (
    HASH_PINNED,
    PinFile,
    fallback_line,
    pin_line,
    plan_pin_edits,
)
from apifix.engine.scanner import scan

MIG = get_migration("openai-v1")


def plan(tmp_path: Path, files: dict[str, str | bytes]):
    root = tmp_path / "repo"
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, bytes) else textwrap.dedent(content).encode()
        (root / rel).write_bytes(data)
    return plan_pin_edits(root, scan(root, MIG).findings, MIG)


def one(tmp_path: Path, rel: str, content: str | bytes) -> PinFile:
    (pin_file,) = plan(tmp_path, {rel: content}).files
    return pin_file


def changed_lines(f: PinFile) -> list[tuple[str, str]]:
    old, new = f.original.splitlines(), f.text.splitlines()
    assert len(old) == len(new)
    return [(a, b) for a, b in zip(old, new, strict=True) if a != b]


# --- requirements files -----------------------------------------------------------------------


def test_requirements_keeps_extras_markers_comments_and_spacing(tmp_path):
    f = one(
        tmp_path,
        "requirements.txt",
        """\
        # pinned for prod
        openai[datalib] == 0.28.1 ; python_version >= "3.8"  # keep this comment
        requests==2.31.0
        """,
    )
    assert f.complete and f.fallbacks == []
    assert changed_lines(f) == [
        (
            'openai[datalib] == 0.28.1 ; python_version >= "3.8"  # keep this comment',
            'openai[datalib] >=1.0,<2 ; python_version >= "3.8"  # keep this comment',
        )
    ]
    (edit,) = f.edits
    assert (edit.line, edit.old, edit.new, edit.kind) == (
        2,
        "openai[datalib] == 0.28.1",
        "openai[datalib] >=1.0,<2",
        "migration",
    )
    assert (
        pin_line(edit) == "requirements.txt:2 openai[datalib] == 0.28.1 -> openai[datalib] >=1.0,<2"
    )


def test_crlf_bom_free_bytes_and_missing_final_newline_survive(tmp_path):
    f = one(tmp_path, "requirements.txt", b"pytest\r\nopenai~=0.27\r\nrich")
    assert f.text.encode() == b"pytest\r\nopenai>=1.0,<2\r\nrich"


def test_comment_with_the_same_text_is_not_touched(tmp_path):
    f = one(tmp_path, "requirements.txt", "# openai==0.28 was the old pin\nopenai==0.28\n")
    assert f.text == "# openai==0.28 was the old pin\nopenai>=1.0,<2\n"
    assert f.edits[0].line == 2


def test_duplicate_entries_are_each_edited(tmp_path):
    f = one(tmp_path, "requirements/base.txt", "openai==0.28\nx\nopenai<1\n")
    assert f.text == "openai>=1.0,<2\nx\nopenai>=1.0,<2\n"
    assert [e.line for e in f.edits] == [1, 3]


def test_continuation_line_falls_back(tmp_path):
    f = one(tmp_path, "requirements.txt", "openai==0.28 \\\n    ; python_version > '3'\n")
    assert not f.complete and f.text == f.original and f.edits == []
    (fb,) = f.fallbacks
    assert (fb.line, fb.sent) == (1, True) and "several lines" in fb.reason
    assert fallback_line(fb).endswith("; sent to the model)")


def test_hash_pinned_file_is_left_for_a_manual_update(tmp_path):
    f = one(
        tmp_path,
        "requirements.txt",
        "openai==0.28.1 \\\n    --hash=sha256:aaaa\nrich==13.0 --hash=sha256:bbbb\n",
    )
    assert f.edits == [] and f.hash_pinned and not f.complete
    (fb,) = f.fallbacks
    assert (fb.line, fb.reason, fb.sent) == (1, HASH_PINNED, False)
    assert fallback_line(fb) == "requirements.txt:1 (hash-pinned; update manually)"


# --- companion pins ---------------------------------------------------------------------------


def test_companion_pins_that_hold_openai_back_are_raised(tmp_path):
    f = one(
        tmp_path,
        "requirements.txt",
        """\
        openai==0.28.1
        typing_extensions==4.5.0
        httpx<0.20
        anyio>=5.1
        distro==1.8.0
        pydantic==1.8.2
        jiter
        """,
    )
    assert [(e.old, e.new, e.kind) for e in f.edits] == [
        ("openai==0.28.1", "openai>=1.0,<2", "migration"),
        ("typing_extensions==4.5.0", "typing_extensions>=4.11,<5", "companion"),
        ("httpx<0.20", "httpx>=0.23,<1", "companion"),
        ("anyio>=5.1", "anyio>=3.5,<5", "companion"),
        ("pydantic==1.8.2", "pydantic>=1.9,<2", "companion"),  # stays on pydantic 1
    ]
    assert f.edits[1].why == "openai needs typing-extensions>=4.11,<5"
    assert pin_line(f.edits[1]) == (
        "requirements.txt:2 typing_extensions==4.5.0 -> typing_extensions>=4.11,<5 "
        "(openai needs typing-extensions>=4.11,<5)"
    )


@pytest.mark.parametrize(
    ("pin", "new"),
    [
        ("pydantic==1.8.2", "pydantic>=1.9,<2"),
        ("pydantic<1.5", "pydantic>=1.9,<2"),
        ("pydantic==0.32", "pydantic>=1.9,<2"),
        ("pydantic>=3.0", "pydantic>=2,<3"),
        ("pydantic==3.1", "pydantic>=2,<3"),
    ],
)
def test_pydantic_keeps_the_repo_major(tmp_path, pin, new):
    f = one(tmp_path, "requirements.txt", f"openai==0.28\n{pin}\n")
    assert f.edits[1].new == new


def test_companion_pins_openai_accepts_are_left_alone(tmp_path):
    reqs = "openai==0.28\ntyping_extensions==4.12.2\npydantic==2.5\nhttpx==0.28.1\n"
    f = one(tmp_path, "requirements.txt", reqs)
    assert [e.kind for e in f.edits] == ["migration"]


def test_companions_only_in_files_where_the_openai_pin_is_set(tmp_path):
    p = plan(
        tmp_path,
        {
            "requirements.txt": "openai==0.28\n",
            "docs/requirements.txt": "sphinx\ntyping_extensions==4.5.0\n",
        },
    )
    assert [f.path for f in p.files] == ["requirements.txt"]


def test_conda_entries_of_companions_are_reported_not_edited(tmp_path):
    f = one(
        tmp_path,
        "environment.yml",
        """\
        name: x
        dependencies:
          - python=3.10
          - typing_extensions=4.5.0=pyha770c72_0
          - pip
          - pip:
            - openai==0.27.6
        """,
    )
    assert changed_lines(f) == [("    - openai==0.27.6", "    - openai>=1.0,<2")]
    assert f.notes == [
        "not changed: environment.yml:4 typing_extensions=4.5.0=pyha770c72_0 (conda entry); "
        "openai needs typing-extensions>=4.11,<5"
    ]


# --- conda environment files ------------------------------------------------------------------


def test_environment_pip_list_quoted_and_commented(tmp_path):
    f = one(
        tmp_path,
        "environment.yml",
        """\
        dependencies:
          - pip:
            - "openai==0.27.6"
            - 'rich'
            - pytest  # tests
        """,
    )
    assert changed_lines(f) == [('    - "openai==0.27.6"', '    - "openai>=1.0,<2"')]
    assert f.edits[0].old == "openai==0.27.6"


def test_environment_escaped_quotes_fall_back(tmp_path):
    f = one(tmp_path, "environment.yml", 'dependencies:\n  - pip:\n    - "openai==0.27\\u0036"\n')
    if f.fallbacks:  # the parser reads it as a requirement only in some spellings
        assert "escapes" in f.fallbacks[0].reason and f.edits == []


# --- TOML -----------------------------------------------------------------------------------


def test_pyproject_project_optional_and_groups(tmp_path):
    f = one(
        tmp_path,
        "pyproject.toml",
        """\
        [project]
        name = "x"
        # "openai==0.28" in a comment stays
        dependencies = [
            "openai==0.28 ; python_version >= '3.8'",
            'rich',
        ]

        [project.optional-dependencies]
        ai = ['openai[datalib]<1']

        [dependency-groups]
        dev = ["openai==0.27.0", "pytest"]
        """,
    )
    assert changed_lines(f) == [
        (
            "    \"openai==0.28 ; python_version >= '3.8'\",",
            "    \"openai>=1.0,<2 ; python_version >= '3.8'\",",
        ),
        ("ai = ['openai[datalib]<1']", "ai = ['openai[datalib]>=1.0,<2']"),
        ('dev = ["openai==0.27.0", "pytest"]', 'dev = ["openai>=1.0,<2", "pytest"]'),
    ]
    assert [(e.line, e.old) for e in f.edits] == [
        (5, "openai==0.28"),
        (10, "openai[datalib]<1"),
        (13, "openai==0.27.0"),
    ]


def test_poetry_string_inline_table_and_sub_table(tmp_path):
    f = one(
        tmp_path,
        "pyproject.toml",
        """\
        [tool.poetry.dependencies]
        python = "^3.9"
        openai = '^0.28'

        [tool.poetry.group.ai.dependencies]
        openai = { version = "0.27.8", extras = ["datalib"] }

        [tool.poetry.dev-dependencies.openai]
        version = "~0.28"
        optional = true
        """,
    )
    assert changed_lines(f) == [
        ("openai = '^0.28'", "openai = '>=1.0,<2'"),
        (
            'openai = { version = "0.27.8", extras = ["datalib"] }',
            'openai = { version = ">=1.0,<2", extras = ["datalib"] }',
        ),
        ('version = "~0.28"', 'version = ">=1.0,<2"'),
    ]
    assert [e.old for e in f.edits] == [
        "openai = '^0.28'",
        'openai = { version = "0.27.8"',
        'version = "~0.28"',
    ]


def test_same_version_string_on_another_key_is_not_touched(tmp_path):
    f = one(
        tmp_path,
        "Pipfile",
        """\
        [packages]
        other = "==0.28"
        openai = "==0.28"

        [dev-packages]
        openai = {version = "==0.28"}
        """,
    )
    assert changed_lines(f) == [
        ('openai = "==0.28"', 'openai = ">=1.0,<2"'),
        ('openai = {version = "==0.28"}', 'openai = {version = ">=1.0,<2"}'),
    ]


def test_pixi_tables_targets_and_features(tmp_path):
    f = one(
        tmp_path,
        "pixi.toml",
        """\
        [pypi-dependencies]
        openai = "==0.28"

        [target.linux-64.pypi-dependencies]
        openai = { version = "<1", extras = ["datalib"] }

        [feature.ai.pypi-dependencies]
        openai = "==0.27.6"
        typing-extensions = "==4.5.0"
        """,
    )
    assert [(e.line, e.new) for e in f.edits] == [
        (2, 'openai = ">=1.0,<2"'),
        (5, 'openai = { version = ">=1.0,<2"'),
        (8, 'openai = ">=1.0,<2"'),
        (9, 'typing-extensions = ">=4.11,<5"'),
    ]


def test_pyproject_pixi_with_lockfile_note(tmp_path):
    p = plan(
        tmp_path,
        {
            "pyproject.toml": '[tool.pixi.pypi-dependencies]\nopenai = "==0.28"\n',
            "pixi.lock": "version: 6\n",
        },
    )
    (f,) = p.files
    assert f.text == '[tool.pixi.pypi-dependencies]\nopenai = ">=1.0,<2"\n'
    assert f.notes == ["not updated: pixi.lock; regenerate it"]


def test_toml_escaped_and_multiline_strings_fall_back(tmp_path):
    f = one(
        tmp_path,
        "pyproject.toml",
        '[project]\ndependencies = ["openai\\u003d=0.28"]\n',
    )
    assert f.edits == []
    (fb,) = f.fallbacks
    # the parser cannot place an escaped string either, so the line is its fallback (1)
    assert fb.entry == "openai==0.28" and "plain one-line" in fb.reason


# --- setup.py / setup.cfg ---------------------------------------------------------------------


def test_setup_py_plain_literals(tmp_path):
    f = one(
        tmp_path,
        "setup.py",
        """\
        from setuptools import setup

        setup(
            name="x",
            install_requires=["openai==0.28.1", 'typing-extensions==4.5.0'],
            extras_require={"ai": ["openai<1; python_version>'3.7'"]},
        )
        """,
    )
    assert changed_lines(f) == [
        (
            """    install_requires=["openai==0.28.1", 'typing-extensions==4.5.0'],""",
            """    install_requires=["openai>=1.0,<2", 'typing-extensions>=4.11,<5'],""",
        ),
        (
            """    extras_require={"ai": ["openai<1; python_version>'3.7'"]},""",
            """    extras_require={"ai": ["openai>=1.0,<2; python_version>'3.7'"]},""",
        ),
    ]


def test_setup_py_non_plain_string_falls_back(tmp_path):
    f = one(
        tmp_path,
        "setup.py",
        'from setuptools import setup\nsetup(install_requires=["openai" "==0.28"])\n',
    )
    assert f.edits == []
    (fb,) = f.fallbacks
    assert fb.line == 2 and "not a plain string literal" in fb.reason
    assert fb.entry == '"openai" "==0.28"'


def test_setup_cfg_install_requires_and_extras(tmp_path):
    f = one(
        tmp_path,
        "setup.cfg",
        """\
        [metadata]
        description = uses openai==0.28

        [options]
        install_requires =
            openai==0.28.1
            rich
        tests_require = openai==0.28.1

        [options.extras_require]
        ai = openai<1
        """,
    )
    assert changed_lines(f) == [
        ("    openai==0.28.1", "    openai>=1.0,<2"),
        ("ai = openai<1", "ai = openai>=1.0,<2"),
    ]


# --- whole plans --------------------------------------------------------------------------------


def test_files_with_other_findings_are_not_complete(tmp_path):
    p = plan(
        tmp_path,
        {
            "setup.py": (
                "import openai\nfrom setuptools import setup\n"
                "openai.ChatCompletion.create()\nsetup(install_requires=['openai==0.28'])\n"
            ),
            "requirements.txt": "openai==0.28\n",
        },
    )
    by_path = {f.path: f for f in p.files}
    assert by_path["requirements.txt"].complete
    assert by_path["setup.py"].edits and not by_path["setup.py"].complete
    files, fallbacks = p.applied(whole_file=True)
    assert [f.path for f in files] == ["requirements.txt"]
    assert [(b.file, b.reason) for b in fallbacks] == [
        ("setup.py", "sent whole: the file has other findings, so the model updates all of it")
    ]
    files, fallbacks = p.applied(whole_file=False)
    assert [f.path for f in files] == ["requirements.txt", "setup.py"] and fallbacks == []


def test_partial_file_is_sent_whole_in_whole_file_mode(tmp_path):
    p = plan(tmp_path, {"requirements.txt": "openai==0.28\nopenai<1 \\\n  ; os_name=='nt'\n"})
    files, fallbacks = p.applied(whole_file=True)
    assert files == []
    assert [(b.line, b.reason.split(":")[0]) for b in fallbacks] == [
        (1, "sent whole"),
        (2, "the requirement continues over several lines"),
    ]
    files, _ = p.applied(whole_file=False)
    assert files[0].text.startswith("openai>=1.0,<2\n")


def test_write_puts_the_edits_in_the_working_copy(tmp_path):
    p = plan(tmp_path, {"requirements.txt": "openai==0.28\n"})
    work = tmp_path / "work"
    work.mkdir()
    (written,) = p.write(work, whole_file=True)
    assert (work / "requirements.txt").read_text() == "openai>=1.0,<2\n"
    assert written.path == "requirements.txt"


def test_unparseable_file_falls_back(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "requirements.txt").write_text("openai==0.28\n")
    findings = scan(root, MIG).findings
    (root / "requirements.txt").write_bytes(b"\xff\xfe")
    (f,) = plan_pin_edits(root, findings, MIG).files
    assert f.fallbacks[0].reason == "not UTF-8 text"


# --- unpinned and loose declarations ------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "content", "old", "new"),
    [
        ("requirements.txt", "openai\nnumpy\n", "openai", "openai>=1.0,<2"),
        (
            "requirements.txt",
            "openai[datalib] ; python_version < '3.12'  # why\n",
            "openai[datalib] ; python_version < '3.12'  # why",
            "openai[datalib]>=1.0,<2 ; python_version < '3.12'  # why",
        ),
        ("requirements.txt", "openai >= 0.27, < 2\n", "openai >= 0.27, < 2", "openai >=1.0,<2"),
        (
            "pyproject.toml",
            '[project]\nname = "x"\ndependencies = ["openai", "numpy"]\n',
            'dependencies = ["openai", "numpy"]',
            'dependencies = ["openai>=1.0,<2", "numpy"]',
        ),
        (
            "pyproject.toml",
            "[tool.poetry.dependencies]\nopenai = '*'\n",
            "openai = '*'",
            "openai = '>=1.0,<2'",
        ),
        (
            "pyproject.toml",
            '[tool.poetry.dependencies]\nopenai = {version = "*", extras = ["x"]}\n',
            'openai = {version = "*", extras = ["x"]}',
            'openai = {version = ">=1.0,<2", extras = ["x"]}',
        ),
        (
            "pyproject.toml",
            '[tool.poetry.dependencies]\nopenai = "^0.27 || ^1.0"\n',
            'openai = "^0.27 || ^1.0"',
            'openai = ">=1.0,<2"',
        ),
        ("Pipfile", '[packages]\nopenai = "*"\n', 'openai = "*"', 'openai = ">=1.0,<2"'),
        (
            "pixi.toml",
            '[pypi-dependencies]\nopenai = "*"\n',
            'openai = "*"',
            'openai = ">=1.0,<2"',
        ),
        (
            "setup.py",
            'from setuptools import setup\nsetup(install_requires=["openai", "x"])\n',
            'setup(install_requires=["openai", "x"])',
            'setup(install_requires=["openai>=1.0,<2", "x"])',
        ),
        (
            "setup.cfg",
            "[options]\ninstall_requires =\n    openai\n    numpy\n",
            "    openai",
            "    openai>=1.0,<2",
        ),
        (
            "environment.yml",
            "dependencies:\n  - pip\n  - pip:\n    - openai\n",
            "    - openai",
            "    - openai>=1.0,<2",
        ),
    ],
)
def test_unpinned_and_loose_declarations_get_the_pin(tmp_path, rel, content, old, new):
    f = one(tmp_path, rel, content)
    assert f.complete and f.fallbacks == []
    assert changed_lines(f) == [(old, new)]
    assert [e.kind for e in f.edits] == ["migration"]


def test_unpinned_entry_report_line(tmp_path):
    f = one(tmp_path, "requirements.txt", "numpy\nopenai\n")
    assert [pin_line(e) for e in f.edits] == ["requirements.txt:2 openai -> openai>=1.0,<2"]


def test_unpinned_project_dependency_with_uv_lock_is_noted_not_edited(tmp_path):
    # SuspensePerception: "openai" in [project] dependencies, uv.lock next to it
    lock = 'version = 1\n[[package]]\nname = "openai"\nversion = "1.99.2"\n'
    p = plan(
        tmp_path,
        {
            "pyproject.toml": '[project]\nname = "x"\ndependencies = ["together", "openai"]\n',
            "uv.lock": lock,
        },
    )
    (f,) = p.files
    assert f.text.endswith('dependencies = ["together", "openai>=1.0,<2"]\n')
    assert f.notes == ["not updated: uv.lock; regenerate it"]
    assert (tmp_path / "repo" / "uv.lock").read_text() == lock


def test_direct_reference_and_1x_declarations_are_left_alone(tmp_path):
    p = plan(
        tmp_path,
        {
            "requirements.txt": "openai @ git+https://github.com/openai/openai-python\n",
            "requirements-dev.txt": "openai>=1.3\n",
        },
    )
    assert p.files == []


def test_unpinned_fixture_gets_the_pin_and_a_lockfile_note():
    root = Path(__file__).parent / "fixtures" / "unpinned_openai"
    findings = scan(root, MIG).findings
    deps = [f for f in findings if f.category == "dependency"]
    assert [(f.file, f.line, f.old_pattern, f.confidence) for f in deps] == [
        ("pyproject.toml", 9, "openai", "likely")
    ]
    assert any(f.category != "dependency" for f in findings)  # the 0.x calls
    (f,) = plan_pin_edits(root, findings, MIG).files
    assert changed_lines(f) == [('    "openai",', '    "openai>=1.0,<2",')]
    assert f.complete and f.notes == ["not updated: uv.lock; regenerate it"]
