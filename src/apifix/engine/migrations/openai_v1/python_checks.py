"""AST checks for openai 0.x usage in one Python file.

Every name is resolved through the file's imports to a canonical dotted path
(``oa.ChatCompletion.create`` -> ``openai.ChatCompletion.create``), so only things that really
come from ``openai`` are reported. Limitations are listed in CLAUDE.md.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from apifix.engine.migrations.openai_v1 import rules
from apifix.engine.models import CERTAIN, LIKELY, Category, Confidence, Finding
from apifix.engine.tampering import is_test_file

_MAX_PATTERN = 120


@dataclass(frozen=True)
class _Rule:
    category: Category
    suggestion: str
    confidence: Confidence
    # False when a bare imported name stays valid once its import is fixed
    # (e.g. `RateLimitError` after `from openai import RateLimitError`).
    name_changes: bool = True


def check_python(file: str, tree: ast.Module, source: str) -> list[Finding]:
    aliases = _collect_aliases(tree)
    visitor = _Visitor(file, source, aliases, azure=_targets_azure(tree, aliases))
    visitor.visit(tree)
    return visitor.findings


# --- name resolution ------------------------------------------------------------------------


def _is_openai(module: str) -> bool:
    return module == "openai" or module.startswith("openai.")


def _collect_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> canonical dotted path, for every name bound by an openai import."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if not _is_openai(a.name):
                    continue
                if a.asname:
                    aliases[a.asname] = a.name
                else:  # `import openai.error` binds `openai`
                    aliases[a.name.split(".")[0]] = "openai"
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and _is_openai(node.module or ""):
            for a in node.names:
                if a.name != "*":
                    aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _resolve(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def _targets_azure(tree: ast.Module, aliases: dict[str, str]) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node.value.value.lower().startswith("azure")
            and any(_resolve(t, aliases) == "openai.api_type" for t in node.targets)
        ):
            return True
    return False


# --- classification -------------------------------------------------------------------------


def _resource_rule(resource: str, method: str | None, azure: bool) -> _Rule:
    if resource in rules.REMOVED_RESOURCES:
        return _Rule("api_call", rules.REMOVED_RESOURCES[resource], CERTAIN)
    path = rules.RESOURCES[resource]
    client = rules.AZURE_CLIENT_ASSIGN if azure else rules.CLIENT
    async_client = rules.ASYNC_AZURE_CLIENT_ASSIGN if azure else rules.ASYNC_CLIENT
    if method is None:
        return _Rule(
            "api_call", f"Resource classes are gone: use client.{path} ({client})", CERTAIN
        )
    is_async = method.startswith("a") and method[1:] in rules.SYNC_METHODS
    sync = method[1:] if is_async else method
    target = f"client.{path}.{rules.METHOD_RENAMES.get((resource, sync), sync)}"
    if is_async:
        suggestion = f"Use await {target}(...) with {async_client}"
    else:
        suggestion = f"Use {target}(...) with {client}"
    return _Rule("api_call", suggestion + "; the response is a typed object, not a dict", CERTAIN)


def _error_rule(name: str) -> _Rule:
    if name not in rules.ERROR_CLASSES:
        return _Rule("error_class", "openai.error is gone: error classes live at openai.*", LIKELY)
    new, note, confidence = rules.ERROR_CLASSES[name]
    if new is None:
        return _Rule("error_class", note, confidence)
    suggestion = f"Use openai.{new}" + (f"; {note}" if note else "")
    return _Rule("error_class", suggestion, confidence, name_changes=new != name)


def _classify(canonical: str, azure: bool) -> _Rule | None:
    parts = canonical.split(".")
    if parts[0] != "openai" or len(parts) < 2:
        return None
    head = parts[1]
    if head in rules.RESOURCES or head in rules.REMOVED_RESOURCES:
        return _resource_rule(head, parts[2] if len(parts) > 2 else None, azure)
    if head == "error":
        if len(parts) == 2:
            return _Rule(
                "error_class", "openai.error is gone: error classes live at openai.*", CERTAIN
            )
        return _error_rule(parts[2])
    if head in rules.TOP_LEVEL_ERROR_REEXPORTS:
        return _error_rule(head)
    if head in rules.REMOVED_MODULES:
        return _Rule("removed_module", rules.REMOVED_MODULES[head], CERTAIN)
    if head in rules.CONFIG_ATTRS and len(parts) == 2:
        plain, for_azure, confidence = rules.CONFIG_ATTRS[head]
        return _Rule("client_config", for_azure if azure else plain, confidence)
    return None


def _unwrap_chain(node: ast.AST) -> tuple[ast.AST, list[ast.Subscript], bool]:
    """Strip subscripts/attributes: (root, subscripts outermost-first, any string key?)."""
    subscripts: list[ast.Subscript] = []
    while isinstance(node, ast.Subscript | ast.Attribute):
        if isinstance(node, ast.Subscript):
            subscripts.append(node)
        node = node.value
    has_key = any(
        isinstance(s.slice, ast.Constant) and isinstance(s.slice.value, str) for s in subscripts
    )
    return node, subscripts, has_key


# --- visitor --------------------------------------------------------------------------------


class _Visitor(ast.NodeVisitor):
    def __init__(self, file: str, source: str, aliases: dict[str, str], azure: bool) -> None:
        self.file = file
        self.source = source
        self.aliases = aliases
        self.azure = azure
        self.test_file = is_test_file(file)
        self.findings: list[Finding] = []
        # One dict per function/class scope: name -> True if it holds a stream, else False.
        self.scopes: list[dict[str, bool]] = [{}]

    # reporting

    def _report(
        self, node: ast.AST, category: Category, suggestion: str, confidence: Confidence
    ) -> None:
        segment = ast.get_source_segment(self.source, node) or ""
        pattern = " ".join(segment.split())
        if len(pattern) > _MAX_PATTERN:
            pattern = pattern[: _MAX_PATTERN - 3] + "..."
        self.findings.append(
            Finding(
                file=self.file,
                line=node.lineno,
                col=node.col_offset + 1,
                end_line=node.end_lineno or node.lineno,
                end_col=(node.end_col_offset or node.col_offset) + 1,
                category=category,
                old_pattern=pattern,
                suggestion=suggestion,
                confidence=confidence,
            )
        )

    def _report_rule(self, node: ast.AST, rule: _Rule, category: Category | None = None) -> None:
        self._report(node, category or rule.category, rule.suggestion, rule.confidence)

    # imports

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if _is_openai(a.name) and (rule := _classify(a.name, self.azure)):
                self._report_rule(a, rule, self._import_category(rule))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level or not _is_openai(module):
            return
        module_rule = _classify(module, self.azure)
        if module_rule and module_rule.category == "removed_module":
            self._report_rule(node, module_rule)
            return
        for a in node.names:
            rule = _classify(f"{module}.{a.name}", self.azure)
            if rule is None:
                continue
            suggestion = rule.suggestion
            if rule.category == "error_class":
                new = rules.ERROR_CLASSES.get(a.name, (None,))[0]
                if new:
                    suggestion = f"from openai import {new}" + suggestion.removeprefix(
                        f"Use openai.{new}"
                    )
            self._report(a, self._import_category(rule), suggestion, rule.confidence)

    @staticmethod
    def _import_category(rule: _Rule) -> Category:
        return "removed_module" if rule.category == "removed_module" else "import"

    # names and attribute chains

    def visit_Attribute(self, node: ast.Attribute) -> None:
        canonical = _resolve(node, self.aliases)
        if canonical and (rule := _classify(canonical, self.azure)):
            self._report_rule(node, rule)
            return  # the rest of the chain is part of this finding
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        canonical = self.aliases.get(node.id)
        if canonical and (rule := _classify(canonical, self.azure)) and rule.name_changes:
            self._report_rule(node, rule)

    # string references, e.g. mock.patch("openai.ChatCompletion.create")

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if not (isinstance(value, str) and value.startswith("openai.")):
            return
        if not all(p.isidentifier() for p in value.split(".")):
            return  # prose, not a dotted path
        rule = _classify(value, self.azure)
        if rule and rule.category in {"api_call", "error_class"}:
            self._report(
                node,
                rule.category,
                f"String reference (e.g. a mock.patch target): {rule.suggestion}",
                LIKELY,
            )

    # calls: kwargs, response tracking

    def _resource_call(self, node: ast.AST) -> ast.Call | None:
        """The openai resource-method call inside `node` (unwrapping await), if any."""
        if isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Call):
            canonical = _resolve(node.func, self.aliases)
            if canonical:
                parts = canonical.split(".")
                if len(parts) == 3 and parts[1] in rules.RESOURCES:
                    return node
        return None

    @staticmethod
    def _is_stream(call: ast.Call) -> bool:
        return any(
            kw.arg == "stream" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in call.keywords
        )

    def _response_kind(self, node: ast.AST) -> bool | None:
        """None if `node` is not a 0.x response (or part of one); else True if from a stream."""
        node, _, _ = _unwrap_chain(node)
        if isinstance(node, ast.Name):
            return self.scopes[-1].get(node.id)
        call = self._resource_call(node)
        return self._is_stream(call) if call else None

    def visit_Call(self, node: ast.Call) -> None:
        call = self._resource_call(node)
        if call is not None:
            for kw in call.keywords:
                if kw.arg in rules.KWARG_CHANGES:
                    self._report(kw, "api_call", rules.KWARG_CHANGES[kw.arg], CERTAIN)
        func = node.func
        if isinstance(func, ast.Attribute):
            # A string-keyed chain before .get() is already reported by visit_Subscript.
            _, _, keyed = _unwrap_chain(func.value)
            dict_call = func.attr in rules.DICT_METHODS and not keyed
            kind = self._response_kind(func.value) if dict_call else None
            if kind is not None:
                message = rules.STREAM_ACCESS if kind else rules.RESPONSE_ACCESS
                self._report(node, "response_access", message, LIKELY)
            elif (
                isinstance(func.value, ast.Name)
                and func.value.id == "json"
                and func.attr in {"dump", "dumps"}
                and node.args
                and self._response_kind(node.args[0]) is not None
            ):
                self._report(node, "response_access", rules.JSON_DUMP, LIKELY)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        cur, chain, has_key = _unwrap_chain(node)
        kind = self._response_kind(cur) if has_key else None
        if kind is None:
            self.generic_visit(node)
            return
        self._report(
            node, "response_access", rules.STREAM_ACCESS if kind else rules.RESPONSE_ACCESS, LIKELY
        )
        # Visit the root (it may be an openai call) and the keys, but not the inner subscripts,
        # which are part of this finding.
        self.visit(cur)
        for s in chain:
            self.visit(s.slice)

    # bindings

    def _bind(self, target: ast.AST, kind: bool | None) -> None:
        if isinstance(target, ast.Name):
            if kind is None:
                self.scopes[-1].pop(target.id, None)
            else:
                self.scopes[-1][target.id] = kind

    def visit_Assign(self, node: ast.Assign) -> None:
        self.generic_visit(node)
        kind = self._response_kind(node.value)
        for t in node.targets:
            self._bind(t, kind)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self._bind(node.target, self._response_kind(node.value))

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        # Iterating a stream yields chunks, which are dict-like in 0.x as well.
        self._bind(node.target, True if self._response_kind(node.iter) else None)
        for stmt in node.body + node.orelse:
            self.visit(stmt)

    visit_For = _visit_for
    visit_AsyncFor = _visit_for

    # test files: mock data shaped like a 0.x (dict) response

    @staticmethod
    def _key(node: ast.expr | None) -> object:
        return node.value if isinstance(node, ast.Constant) else None

    def _is_response_dict(self, node: ast.Dict) -> bool:
        """{"choices": [{"message": ...}]} or {"choices": [{"text": ...}]} (list or tuple)."""
        for key, value in zip(node.keys, node.values, strict=True):
            if self._key(key) != rules.RESPONSE_DICT_KEY:
                continue
            if isinstance(value, ast.List | ast.Tuple):
                for elt in value.elts:
                    if isinstance(elt, ast.Dict) and any(
                        self._key(k) in rules.RESPONSE_CHOICE_KEYS for k in elt.keys
                    ):
                        return True
        return False

    def visit_Dict(self, node: ast.Dict) -> None:
        if self.test_file and self._is_response_dict(node):
            self._report(node, "response_access", rules.MOCK_DICT_RESPONSE, LIKELY)
        self.generic_visit(node)

    def _visit_scope(self, node: ast.AST) -> None:
        self.scopes.append({})
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope
    visit_Lambda = _visit_scope
