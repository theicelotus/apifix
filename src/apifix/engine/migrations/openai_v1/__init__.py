"""openai 0.x -> 1.x migration."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from apifix.engine.dependencies import (
    DeclaredDependency,
    entirely_at_or_above,
    entirely_below,
)
from apifix.engine.migrations.guide import full_guide
from apifix.engine.migrations.openai_v1 import config_checks, guide_parts, repo_findings, rules
from apifix.engine.migrations.openai_v1.python_checks import (
    check_python,
    stream_check_errors,
    v1_api_uses,
)
from apifix.engine.models import CERTAIN, LIKELY, Finding, SmokeCheck


class OpenAIV1Migration:
    name = "openai-v1"
    summary = "OpenAI Python library 0.x -> 1.x"
    api_modules = frozenset({"openai"})
    broad_exceptions = frozenset({"OpenAIError", "APIError", "APIStatusError"})
    companion_modules = frozenset({"httpx"})
    guide = full_guide(guide_parts.GUIDE_SOURCE)
    # The repo's CI sets these for its tests: the sandbox's test phase gets a placeholder (the
    # network is off, so it cannot be used), and the report says so.
    ci_env_placeholders = {"OPENAI_API_KEY": "sk-apifix-placeholder-not-a-real-key"}
    # (marker in a failing test's output, what it means) for the model's retry feedback.
    failure_hints = (
        (
            "api_key client option must be set",
            "openai.OpenAI() raised because no API key is set: the tests run without one. "
            "Keep the real constructor from running in tests: patch it where the code under "
            'test looks it up (e.g. patch("yourpkg.module.openai.OpenAI") and set '
            "OpenAI.return_value.chat.completions.create.return_value), patch the module's "
            "client or get_client(), or build the client lazily instead of at import. Never "
            "add a real key. If the code uses the module-level client "
            "(openai.chat.completions.create), set a placeholder key in the test "
            '(OPENAI_API_KEY, or openai.api_key = "test").',
        ),
        (
            "unexpected keyword argument 'proxies'",
            "openai releases before 1.55.3 do not work with httpx 0.28 or later. Pin "
            "openai>=1.55.3 (it needs typing_extensions>=4.11, so raise an older pin of that "
            "too), or keep the old openai and pin httpx<0.28.",
        ),
        (
            # agent mode and `apifix verify` see autospec on a client class only here
            # (whole-file mode rejects it before verifying: code_checks.autospec_client_problems)
            "Mock object has no attribute 'completions'",
            'a client class patched with autospec=True (patch("yourpkg.module.OpenAI", '
            "autospec=True)) gives an instance without chat.completions: in 1.x the client's "
            "resources are cached properties, which autospec does not follow. Drop "
            'autospec=True when patching the client class (patch("yourpkg.module.OpenAI")). '
            "Patching the method instead (openai.resources.chat.completions.Completions.create) "
            "leaves the real client being built, so the test must then set a placeholder "
            "OPENAI_API_KEY.",
        ),
    )
    mock_failure_hint = (
        "the test's mock client is incomplete: {problem}; build the client mock with "
        "MagicMock() (or include the whole chat.completions.create chain) and set "
        ".return_value on create"
    )
    uncalled_mock_hint = (
        "a mock was never called, and {where} without api_key=: with no OPENAI_API_KEY set "
        "(the tests run without one) the constructor raises OpenAIError before the call, and "
        "the code's own try/except may swallow the error. Set a placeholder key in the test "
        '(mock.patch.dict(os.environ, {{"OPENAI_API_KEY": "test"}}) or '
        'monkeypatch.setenv("OPENAI_API_KEY", "test")), or patch the client class where the '
        'code looks it up (patch("{target}"), without autospec)'
    )
    uncalled_patched_hint = (
        "a mock was never called in {tests}, which patch the client class, so no real client "
        "is built and no API key is needed: the code under test did not reach the call in "
        "that test. Compare the test with the original: a call assertion the original does not "
        "make (create.assert_called_once() in a test whose code raises or returns before the "
        "call) expects a call the code never makes there; keep the test's assertions as they "
        "were. Otherwise check that the patch target is where the code under test looks the "
        "client up"
    )
    # A client must be constructible with the installed versions (a dummy key; nothing is sent:
    # the network is off and constructing a client makes no request).
    smoke_check = SmokeCheck(
        distribution="openai",
        code=('import openai\nopenai.OpenAI(api_key="sk-apifix-smoke-check-not-a-real-key")\n'),
        description='import openai; openai.OpenAI(api_key="<dummy key>")',
    )

    known_imports = rules.KNOWN_IMPORTS
    mock_responses = rules.MOCK_RESPONSES
    client_classes = rules.CLIENT_CLASSES
    client_resources = rules.CLIENT_RESOURCES
    client_key = rules.CLIENT_KEY
    keyword_renames = rules.KWARG_RENAMES
    package = rules.PACKAGE
    pin_spec = rules.PIN_SPEC
    companion_pins = rules.COMPANION_PINS
    first_version = rules.FIRST_V1
    target_name = "openai 1.x"
    source_name = "openai 0.x"
    pin_reasons = rules.PIN_REASONS
    pr_why = (
        "openai 1.0 removed the module-level API (`openai.ChatCompletion.create`, "
        "`openai.error`, ...), so code written for 0.x fails once `openai>=1` is installed."
    )

    def pr_change(
        self, category: str, old_pattern: str, new_code: str, file_code: str
    ) -> str | None:
        return rules.pr_change(category, old_pattern, new_code, file_code)

    def job_guide(
        self, findings: list[Finding], root: Path | None = None, shared: bool = False
    ) -> tuple[str, list[str]]:
        return guide_parts.job_guide(findings, root, shared)

    def guide_text(self, part_ids: Iterable[str]) -> str:
        return guide_parts.parts_text(part_ids)

    def check_python(
        self,
        file: str,
        tree: ast.Module,
        source: str,
        repo_modules: frozenset[str] = frozenset(),
    ) -> list[Finding]:
        return check_python(file, tree, source, repo_modules)

    def new_api_uses(self, file: str, tree: ast.Module) -> list[tuple[int, str]]:
        return v1_api_uses(tree)

    def adjust_findings(self, findings: list[Finding], root: Path | None = None) -> list[Finding]:
        return repo_findings.adjust_findings(findings, root)

    def rewrite_checker(
        self, root: Path, findings: Sequence[Finding]
    ) -> Callable[[str, str, str], Mapping[str, list[str]]] | None:
        """Checks `config` (config_checks.py: dropped / ignored module configuration) and
        `streaming` (key checks on a stream delta)."""
        config = config_checks.rewrite_checker(root, findings)

        def check(path: str, original: str, candidate: str) -> dict[str, list[str]]:
            return {
                "config": config(path, original, candidate),
                "streaming": stream_check_errors(path, original, candidate),
            }

        return check

    def check_dependency(self, file: str, dep: DeclaredDependency) -> Finding | None:
        """A declaration that allows a 0.x release: `certain` when it allows nothing else,
        `likely` when it allows 1.x too (no version, `*`, `>=0.27`, `<2`). Direct references
        have no version to set."""
        if dep.name != rules.PACKAGE or dep.direct:
            return None
        if entirely_at_or_above(dep.clauses, rules.FIRST_V1):
            return None
        below = entirely_below(dep.clauses, rules.FIRST_V1)
        if below:
            suggestion = rules.DEPENDENCY
        else:
            what = (
                f"`{dep.raw}` still allows 0.x" if dep.clauses else "the declaration has no version"
            )
            suggestion = rules.LOOSE_DEPENDENCY.format(what=what)
        return Finding(
            file=file,
            line=dep.line,
            col=dep.col,
            end_line=dep.line,
            end_col=dep.col + len(dep.raw),
            category="dependency",
            old_pattern=dep.raw,
            suggestion=suggestion,
            confidence=CERTAIN if below else LIKELY,
        )


MIGRATION = OpenAIV1Migration()
