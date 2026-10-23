"""One test file that fails to collect (it imports a package nothing declares) no longer stops
the others: they run in a real sandbox, the module counts as one error per run, and the
comparison treats it as already failing.

Needs Docker and network (installs pytest and coverage only).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from apifix.engine.docker import DockerCLI
from apifix.engine.sandbox import Sandbox, SandboxConfig, default_pip_cache_dir
from apifix.engine.verifier import verify

_usable, _why = DockerCLI().available()
pytestmark = [pytest.mark.docker, pytest.mark.skipif(not _usable, reason=_why or "no docker")]

CONFIG = dict(
    install_timeout_s=600,
    test_timeout_s=180,
    memory="1g",
    cpus=1.0,
    pip_cache_dir=default_pip_cache_dir(),
)

ORIGINAL = {
    "rating.py": """\
        def clamp(score):
            return max(1, min(9, score))
        """,
    "heavy.py": """\
        import nlpaug.augmenter.word as naw


        def augment(text):
            return naw.SynonymAug().augment(text)
        """,
    "tests/test_rating.py": """\
        from rating import clamp


        def test_clamp_low():
            assert clamp(-3) == 1


        def test_clamp_high():
            assert clamp(12) == 9
        """,
    "tests/test_heavy.py": """\
        from heavy import augment


        def test_augment():
            assert augment("x")
        """,
}


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(textwrap.dedent(text))
    return root


@pytest.fixture(scope="module")
def trees(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("collection")
    fixed = {**ORIGINAL, "rating.py": "def clamp(score):\n    return min(9, max(1, score))\n"}
    return write(root / "original", ORIGINAL), write(root / "fixed", fixed)


def test_other_test_files_run_past_a_collection_error(trees):
    original, _ = trees
    result = Sandbox(SandboxConfig(**CONFIG)).run(original)
    assert "--continue-on-collection-errors" in result.test_command
    assert (result.counts.passed, result.counts.errors) == (2, 1), result.reason
    (heavy,) = [t for t in result.tests if t.outcome == "error"]
    assert heavy.id == "tests.test_heavy" and heavy.missing_module == "nlpaug"


def test_module_failing_to_collect_on_both_sides_is_no_signal(trees):
    original, fixed = trees
    v = verify(original, fixed, "openai-v1", sandbox=Sandbox(SandboxConfig(**CONFIG)))
    assert v.delta.no_signal_preexisting == ["tests.test_heavy"]
    assert v.delta.broken_modules == []
    codes = {r.code for r in v.reasons}
    assert "environment_incomplete" not in codes and "collection_error" not in codes
    (r,) = [r for r in v.reasons if r.code == "missing_modules"]
    assert "nlpaug blocks tests/test_heavy.py; the other tests ran" in r.message
    # nothing rejects; the tree has no openai, so the smoke check leaves it unverified
    assert [r.code for r in v.reasons if r.severity in ("reject", "unverified")] == [
        "library_not_declared"
    ]
