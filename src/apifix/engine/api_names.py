"""Dotted module names of a migrated library (`Migration.api_modules`, e.g. `openai`, or
`google.genai` and `google.generativeai`) and the names a file binds to them.

A library module matches itself and everything under it by dotted prefix: `google.genai`
covers `google.genai.types`, but not `google.cloud` or `google.protobuf`, which share only the
top-level package."""

from __future__ import annotations

import ast
from collections.abc import Collection, Mapping


def in_modules(name: str, modules: Collection[str]) -> bool:
    """Is the dotted `name` one of `modules` or inside one?"""
    return any(name == m or name.startswith(f"{m}.") for m in modules)


def module_aliases(tree: ast.AST, modules: Collection[str]) -> dict[str, str]:
    """Local name -> library module, for the imports in `tree` that bind a library module
    under another name: `import google.generativeai as genai`, `from google import genai`,
    `import openai as oa`. Names imported from inside a module (`from openai import OpenAI`)
    are not modules and are left out; a plain `import google.genai` binds `google`, whose
    dotted names (`google.genai.Client`) match as they are written."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname and in_modules(a.name, modules):
                    out[a.asname] = a.name
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for a in node.names:
                full = f"{node.module}.{a.name}"
                if full in modules:
                    out[a.asname or a.name] = full
    return out


def resolve(name: str, aliases: Mapping[str, str]) -> str:
    """`name` with its first part replaced by what it is an alias of (`genai.Client` ->
    `google.genai.Client`)."""
    head, _, rest = name.partition(".")
    base = aliases.get(head)
    if base is None:
        return name
    return f"{base}.{rest}" if rest else base
