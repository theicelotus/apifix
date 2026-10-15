"""openai 0.x -> 1.x migration."""

from __future__ import annotations

import ast

from apifix.engine.dependencies import DeclaredDependency, entirely_below
from apifix.engine.migrations.guide import full_guide
from apifix.engine.migrations.openai_v1 import guide_parts, rules
from apifix.engine.migrations.openai_v1.python_checks import check_python
from apifix.engine.models import CERTAIN, Finding


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
            'test looks it up (e.g. patch("app.openai.OpenAI") and set '
            "OpenAI.return_value.chat.completions.create.return_value), patch the module's "
            "client or get_client(), or build the client lazily instead of at import. Never "
            "add a real key.",
        ),
    )

    def job_guide(self, findings: list[Finding]) -> tuple[str, list[str]]:
        return guide_parts.job_guide(findings)

    def check_python(self, file: str, tree: ast.Module, source: str) -> list[Finding]:
        return check_python(file, tree, source)

    def check_dependency(self, file: str, dep: DeclaredDependency) -> Finding | None:
        if dep.name != rules.PACKAGE or not entirely_below(dep.clauses, rules.FIRST_V1):
            return None
        return Finding(
            file=file,
            line=dep.line,
            col=dep.col,
            end_line=dep.line,
            end_col=dep.col + len(dep.raw),
            category="dependency",
            old_pattern=dep.raw,
            suggestion=rules.DEPENDENCY,
            confidence=CERTAIN,
        )


MIGRATION = OpenAIV1Migration()
