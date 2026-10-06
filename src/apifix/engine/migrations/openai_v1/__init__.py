"""openai 0.x -> 1.x migration."""

from __future__ import annotations

import ast
from pathlib import Path

from apifix.engine.dependencies import DeclaredDependency, entirely_below
from apifix.engine.migrations.openai_v1 import rules
from apifix.engine.migrations.openai_v1.python_checks import check_python
from apifix.engine.models import CERTAIN, Finding


class OpenAIV1Migration:
    name = "openai-v1"
    summary = "OpenAI Python library 0.x -> 1.x"
    api_modules = frozenset({"openai"})
    broad_exceptions = frozenset({"OpenAIError", "APIError", "APIStatusError"})
    companion_modules = frozenset({"httpx"})
    guide = (Path(__file__).parent / "guide.md").read_text(encoding="utf-8")

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
