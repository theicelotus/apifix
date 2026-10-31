"""google-generativeai -> google-genai migration."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from apifix.engine.dependencies import (
    DeclaredDependency,
    entirely_at_or_above,
    entirely_below,
    normalize_name,
)
from apifix.engine.migrations.google_genai import (
    config_checks,
    guide_parts,
    python_checks,
    repo_findings,
    rules,
    test_checks,
)
from apifix.engine.migrations.google_genai.streaming import streaming_check_errors
from apifix.engine.migrations.guide import full_guide
from apifix.engine.models import CERTAIN, LIKELY, Finding, SmokeCheck

_MAX_PATTERN = 80


def _inline(text: str) -> str:
    text = " ".join(text.replace("`", "'").split())
    return "`" + (text[: _MAX_PATTERN - 1] + "…" if len(text) > _MAX_PATTERN else text) + "`"


class GoogleGenAIMigration:
    name = "google-genai"
    summary = "google-generativeai -> Google Gen AI SDK (google-genai)"
    api_modules = frozenset({"google.generativeai", "google.genai"})
    broad_exceptions = frozenset({"APIError", "GoogleAPIError", "GoogleAPICallError"})
    companion_modules = frozenset({"google.api_core"})
    guide = full_guide(guide_parts.GUIDE_SOURCE)
    ci_env_placeholders = {
        "GOOGLE_API_KEY": "apifix-placeholder-not-a-real-key",
        "GEMINI_API_KEY": "apifix-placeholder-not-a-real-key",
    }
    failure_hints = (
        (
            "No API key was provided",
            "genai.Client() raised because no API key is set: the tests run without one. "
            "Keep the real constructor from running in tests: patch it where the code under "
            'test looks it up (e.g. patch("yourpkg.module.genai.Client") and set '
            "Client.return_value.models.generate_content.return_value), or build the client "
            "lazily instead of at import. Never add a real key. Setting GOOGLE_API_KEY or "
            "GEMINI_API_KEY to a placeholder in the test also works.",
        ),
        (
            "does not have the attribute 'GenerativeModel'",
            "the test still patches genai.GenerativeModel, which does not exist on the new "
            "SDK's module (GenerativeModel was a class on google.generativeai, not google."
            'genai): patch "yourpkg.module.genai.Client" instead.',
        ),
        (
            "does not have the attribute 'configure'",
            "the test still patches genai.configure, which does not exist on the new SDK's "
            'module: patch "yourpkg.module.genai.Client" instead.',
        ),
        (
            "unexpected keyword argument 'stream'",
            "generate_content/send_message take no stream= in the new SDK: use "
            "generate_content_stream/send_message_stream instead.",
        ),
        (
            "'coroutine' object is not an async iterable",
            "the async stream method itself is a coroutine returning an async iterator: "
            "`async for chunk in await client.aio.models.generate_content_stream(...)`.",
        ),
        (
            "No module named 'google.generativeai'",
            "a leftover import of, or sys.modules stub for, google.generativeai: the new SDK "
            "is google.genai (from google import genai).",
        ),
        (
            "No module named 'google.api_core'",
            "google-api-core is no longer installed with google-genai: move the except clause "
            "to google.genai.errors (ClientError / ServerError / APIError), or keep "
            "google-api-core as a dependency if the file uses it for something else.",
        ),
    )
    mock_failure_hint = (
        "the test's mock client is incomplete: {problem}; build it with MagicMock() (or set "
        ".return_value on Client().models.generate_content)"
    )
    uncalled_mock_hint = (
        "a mock was never called, and {where} without api_key=: with neither GOOGLE_API_KEY "
        "nor GEMINI_API_KEY set (the tests run without one) the constructor raises ValueError "
        "before the call, and the code's own try/except may swallow the error. Set a "
        "placeholder key in the test, or patch the client class where the code looks it up "
        '(patch("{target}"), without autospec)'
    )
    uncalled_patched_hint = (
        "a mock was never called in {tests}, which patch the client class, so no real client "
        "is built and no API key is needed: the code under test did not reach the call in "
        "that test. Compare the test with the original: a call assertion the original does not "
        "make expects a call the code never makes there; keep the test's assertions as they "
        "were. Otherwise check that the patch target is where the code under test looks the "
        "client up"
    )
    smoke_check = SmokeCheck(
        distribution=rules.PACKAGE,
        code=(
            'from google import genai\ngenai.Client(api_key="apifix-smoke-check-not-a-real-key")\n'
        ),
        description='from google import genai; genai.Client(api_key="<dummy key>")',
    )

    known_imports = rules.KNOWN_IMPORTS
    mock_responses = rules.MOCK_RESPONSES
    client_classes = rules.CLIENT_CLASSES
    client_resources = rules.CLIENT_RESOURCES
    client_key = rules.CLIENT_KEY
    keyword_renames = rules.KWARG_RENAMES
    package = rules.PACKAGE
    pin_spec = rules.PIN_SPEC
    source_package = rules.SOURCE_PACKAGE
    key_failure_markers = ("No API key was provided", "GOOGLE_API_KEY", "GEMINI_API_KEY")
    companion_pins = rules.COMPANION_PINS
    first_version = rules.FIRST_VERSION
    target_name = "google-genai"
    source_name = "google-generativeai"
    pin_reasons = rules.PIN_REASONS
    min_python = "3.9"
    pr_why = (
        "google-generativeai is no longer supported (support ended 2025-11-30, its repository "
        "is archived); Google's replacement is google-genai, with a different API "
        "(`genai.Client`, `client.models.generate_content`)."
    )

    def pr_change(
        self, category: str, old_pattern: str, new_code: str, file_code: str
    ) -> str | None:
        del file_code
        if category == "dependency" or not old_pattern or old_pattern in new_code:
            return None
        return f"{_inline(old_pattern)} → migrated"

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
        return python_checks.check_python(file, tree, source, repo_modules)

    def new_api_uses(self, file: str, tree: ast.Module) -> list[tuple[int, str]]:
        del file
        return python_checks.new_api_uses(tree)

    def adjust_findings(self, findings: list[Finding], root: Path | None = None) -> list[Finding]:
        return repo_findings.adjust_findings(findings, root)

    def rewrite_checker(
        self, root: Path, findings: Sequence[Finding]
    ) -> Callable[[str, str, str], Mapping[str, list[str]]] | None:
        """Checks `key` / `settings` / `safety` (config_checks.py), `streaming` (streaming.py)
        and `patches` (test_checks.py)."""
        config = config_checks.rewrite_checker(root, findings)
        patches = test_checks.rewrite_checker(root, findings)

        def check(path: str, original: str, candidate: str) -> dict[str, list[str]]:
            return {
                **config(path, original, candidate),
                "streaming": streaming_check_errors(path, original, candidate),
                "patches": patches(path, original, candidate),
            }

        return check

    def check_dependency(self, file: str, dep: DeclaredDependency) -> Finding | None:
        """`certain` for any declaration of the old distribution (every release is the old
        SDK); for the new one, `certain` when it allows nothing from `first_version` on,
        `likely` when it allows older releases too (no version, `*`, a loose range)."""
        if dep.direct:
            return None
        if dep.name == normalize_name(rules.SOURCE_PACKAGE):
            return Finding(
                file=file,
                line=dep.line,
                col=dep.col,
                end_line=dep.line,
                end_col=dep.col + len(dep.raw),
                category="dependency",
                old_pattern=dep.raw,
                suggestion=rules.SOURCE_DEPENDENCY,
                confidence=CERTAIN,
            )
        if dep.name != normalize_name(rules.PACKAGE):
            return None
        if entirely_at_or_above(dep.clauses, rules.FIRST_VERSION):
            return None
        below = entirely_below(dep.clauses, rules.FIRST_VERSION)
        if below:
            suggestion = rules.DEPENDENCY
        else:
            what = (
                f"`{dep.raw}` still allows an older release"
                if dep.clauses
                else "the declaration has no version"
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


MIGRATION = GoogleGenAIMigration()
