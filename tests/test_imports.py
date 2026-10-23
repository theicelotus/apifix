"""Imported but not declared: third-party imports no dependency file declares."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from apifix.engine.imports import (
    UndeclaredImport,
    required_imports,
    undeclared_imports,
    undeclared_record,
    undeclared_section,
)
from apifix.engine.project import SandboxRequirements

FIXTURE = Path(__file__).parent / "fixtures" / "collection_errors"


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    return root


def names(items: list[UndeclaredImport]) -> list[tuple[str, str, tuple[str, ...]]]:
    return [(i.module, i.distribution, i.files) for i in items]


def test_fixture_lists_each_undeclared_module_once_with_its_files():
    assert names(undeclared_imports(FIXTURE)) == [
        ("cv2", "opencv-python", ("src/suspense/adversarial.py",)),
        ("nlpaug", "nlpaug", ("src/suspense/adversarial.py",)),
        ("PIL", "pillow", ("scripts/plot.py",)),
        ("sklearn", "scikit-learn", ("src/suspense/adversarial.py",)),
        ("textattack", "textattack", ("src/suspense/adversarial.py",)),
    ]
    # not listed: openai, yaml (pyyaml) and dotenv (python-dotenv) are declared; torch and
    # vaderSentiment in extras; ujson is optional (try/except ImportError); pandas only under
    # TYPE_CHECKING; sphinx_rtd_theme only in docs/; src and suspense are the repo's own;
    # pytest and the stdlib are always there


def test_sandbox_requirements_lines_count_as_declared():
    override = SandboxRequirements(
        "/home/u/reqs.txt",
        "0" * 64,
        ("--index-url https://example.invalid/simple", "textattack==0.3.10", "opencv-python"),
    )
    found = [i.module for i in undeclared_imports(FIXTURE, override)]
    assert found == ["nlpaug", "PIL", "sklearn"]


@pytest.mark.parametrize(
    ("module", "declaration", "declared"),
    [
        ("yaml", "pyyaml", True),
        ("dotenv", "python-dotenv", True),
        ("sklearn", "scikit-learn", True),
        ("cv2", "opencv-python-headless", True),
        ("vaderSentiment", "vadersentiment", True),
        ("vaderSentiment", "vaderSentiment==3.3.2", True),
        ("PIL", "Pillow", True),
        ("hydra", "hydra-core", True),
        ("google", "google-cloud-storage", True),
        ("sklearn", "sklearn-crfsuite", False),
    ],
)
def test_import_names_match_differently_named_distributions(
    tmp_path, module, declaration, declared
):
    repo = write(
        tmp_path,
        {"requirements.txt": f"{declaration}\n", "app.py": f"import {module}.sub\n"},
    )
    assert (undeclared_imports(repo) == []) is declared


def test_declarations_in_every_file_and_table_count(tmp_path):
    repo = write(
        tmp_path,
        {
            "pyproject.toml": """\
                [project]
                name = "x"
                dependencies = []
                [dependency-groups]
                dev = ["rich"]
                [tool.pixi.dependencies]
                tomli_w = "*"
                """,
            "requirements/dev.txt": "httpx\n",
            "environment.yml": "dependencies:\n  - numpy\n  - pip:\n    - typer\n",
            "app.py": "import rich, httpx, tomli_w, numpy, typer, requests\n",
        },
    )
    assert [i.module for i in undeclared_imports(repo)] == ["requests"]


def test_tests_and_code_both_count_and_files_are_sorted(tmp_path):
    repo = write(
        tmp_path,
        {
            "tests/test_b.py": "import responses\n",
            "pkg/a.py": "def f():\n    import responses\n",  # inside a function: required
            "tests/test_a.py": "from responses import matchers\n",
        },
    )
    (item,) = undeclared_imports(repo)
    assert item.files == ("pkg/a.py", "tests/test_a.py", "tests/test_b.py")


def test_required_imports_skip_optional_relative_and_type_checking_imports():
    tree = ast.parse(
        textwrap.dedent(
            """\
            import a.b
            from c import d
            from . import local
            from .pkg import thing
            import typing
            try:
                import fast
            except (ImportError, AttributeError):
                import slow
            try:
                import guarded
            except Exception:
                pass
            try:
                import needed
            except ValueError:
                pass
            try:
                import anything
            except:
                pass
            if typing.TYPE_CHECKING:
                import hints
            else:
                import runtime
            """
        )
    )
    assert required_imports(tree) == {"a", "c", "typing", "slow", "needed", "runtime"}


def test_unparseable_files_and_doc_dirs_are_skipped(tmp_path):
    repo = write(
        tmp_path,
        {
            "broken.py": "import nope(\n",
            "docs/conf.py": "import sphinx_rtd_theme\n",
            "doc/source/conf.py": "import furo\n",
            "app/docs.py": "import markdown\n",  # a module named docs, not the docs dir
        },
    )
    assert [i.module for i in undeclared_imports(repo)] == ["markdown"]


def test_section_and_record():
    items = [
        UndeclaredImport("nlpaug", "nlpaug", ("src/a.py",)),
        UndeclaredImport("cv2", "opencv-python", tuple(f"m{n}.py" for n in range(7))),
    ]
    assert undeclared_section(items) == [
        "## Imported but not declared",
        "",
        "Found by reading the code before any sandbox run. Information only: nothing was "
        "installed because of this list. To install them in the sandbox, list them in a "
        "`--sandbox-requirements` file.",
        "",
        "- `nlpaug` (pip: `nlpaug`): src/a.py",
        "- `cv2` (pip: `opencv-python`): m0.py, m1.py, m2.py, m3.py, m4.py (+2 more)",
        "",
    ]
    assert undeclared_section([]) == []
    assert undeclared_record(items[:1]) == [
        {"module": "nlpaug", "distribution": "nlpaug", "files": ["src/a.py"]}
    ]
