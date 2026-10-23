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
from apifix.engine.project import is_test_file

_MAX_PATTERN = 120
_MAX_VALUE = 60


@dataclass(frozen=True)
class _Rule:
    category: Category
    suggestion: str
    confidence: Confidence
    # False when a bare imported name stays valid once its import is fixed
    # (e.g. `RateLimitError` after `from openai import RateLimitError`).
    name_changes: bool = True


def check_python(
    file: str, tree: ast.Module, source: str, repo_modules: frozenset[str] = frozenset()
) -> list[Finding]:
    """`repo_modules`: dotted names of the repo's own modules (``pkg.mod``), so a string such as
    ``"app.openai.X"`` is not taken for the library when the repo has its own ``openai``."""
    aliases = _collect_aliases(tree)
    visitor = _Visitor(file, source, aliases, _targets_azure(tree, aliases), repo_modules)
    visitor.visit(tree)
    return visitor.findings


def v1_api_uses(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, name) of every use of a name only 1.x has (`openai.OpenAI`, `openai.chat`, ...),
    aliases resolved, imports included; sorted, one entry per line and name."""
    aliases = _collect_aliases(tree)
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and _is_openai(node.module or ""):
            for a in node.names:
                if f"{node.module}.{a.name}" in rules.V1_ONLY_NAMES:
                    found.add((node.lineno, f"{node.module}.{a.name}"))
        elif isinstance(node, (ast.Name, ast.Attribute)):
            canonical = _resolve(node, aliases)
            parts = canonical.split(".") if canonical else []
            if canonical in rules.V1_ONLY_NAMES:
                found.add((node.lineno, canonical))
            elif len(parts) > 1 and parts[0] == "openai" and parts[1] in rules.V1_MODULE_RESOURCES:
                found.add((node.lineno, ".".join(parts[:2])))
    return sorted(found)


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


def env_name(node: ast.expr) -> str | None:
    """The variable of `os.getenv("X")`, `os.environ.get("X")` or `os.environ["X"]` (also
    `getenv` / `environ` imported from os), with no default; else None."""
    if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
        arg = node.args[0]
        if (
            ast.unparse(node.func) in ("os.getenv", "getenv", "os.environ.get", "environ.get")
            and isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
        ):
            return arg.value
    if (
        isinstance(node, ast.Subscript)
        and ast.unparse(node.value) in ("os.environ", "environ")
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def config_value_exempt(attr: str, value: ast.expr) -> bool:
    """None, or a read of the variable 1.x reads itself for module attribute `attr`: dropping
    the assignment loses nothing."""
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    env = env_name(value)
    return env is not None and env == rules.ENV_READ_BY_V1.get(attr)


def config_target(target: ast.AST, aliases: dict[str, str]) -> str | None:
    """The module attribute (`api_key`, ...) an assignment target sets on openai, if any."""
    canonical = _resolve(target, aliases) if isinstance(target, ast.Attribute) else None
    parts = canonical.split(".") if canonical else []
    return parts[1] if len(parts) == 2 else None


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
    def __init__(
        self,
        file: str,
        source: str,
        aliases: dict[str, str],
        azure: bool,
        repo_modules: frozenset[str] = frozenset(),
    ) -> None:
        self.file = file
        self.repo_modules = repo_modules
        self.source = source
        self.aliases = aliases
        self.azure = azure
        self.test_file = is_test_file(file, source)
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
            attr = canonical.split(".", 1)[1]
            if attr in ("api_key", "organization") and not self.azure:  # a read
                rule = _Rule(rule.category, rules.CONFIG_READ.format(attr=attr), rule.confidence)
            self._report_rule(node, rule)
            return  # the rest of the chain is part of this finding
        self.generic_visit(node)

    def _config_rule(self, attr: str, value: ast.expr) -> _Rule:
        """The finding for an assignment of `value` to openai.<attr> (CONFIG_ASSIGN_ATTRS)."""
        plain, for_azure, confidence = rules.CONFIG_ATTRS[attr]
        exempt = config_value_exempt(attr, value)
        if self.azure:
            none = isinstance(value, ast.Constant) and value.value is None
            return _Rule(
                "client_config", for_azure + ("" if none else rules.AZURE_CONFIG_KEEP), confidence
            )
        source = " ".join(
            (ast.get_source_segment(self.source, value) or ast.unparse(value)).split()
        )
        if len(source) > _MAX_VALUE:
            source = "..."
        if attr == "api_base":
            return _Rule("client_config", rules.CONFIG_API_BASE.format(value=source), CERTAIN)
        if exempt:
            env = rules.ENV_READ_BY_V1[attr]
            return _Rule("client_config", rules.CONFIG_DROP.format(env=env), confidence)
        return _Rule("client_config", rules.CONFIG_KEEP.format(attr=attr, value=source), confidence)

    def visit_Name(self, node: ast.Name) -> None:
        canonical = self.aliases.get(node.id)
        if canonical and (rule := _classify(canonical, self.azure)) and rule.name_changes:
            self._report_rule(node, rule)

    # string references, e.g. mock.patch("openai.ChatCompletion.create"), also through the module
    # that uses the library: mock.patch("app.openai.ChatCompletion.create")

    def _local_module(self, dotted: str) -> bool:
        return any(m == dotted or m.endswith("." + dotted) for m in self.repo_modules)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if not (isinstance(value, str) and "openai." in value):
            return
        parts = value.split(".")
        if "openai" not in parts or not all(p.isidentifier() for p in parts):
            return  # prose, not a dotted path
        i = parts.index("openai")
        prefix = ".".join(parts[:i])
        if prefix and self._local_module(f"{prefix}.openai"):
            return  # the repo's own module called openai
        rule = _classify(".".join(parts[i:]), self.azure)
        if rule and rule.category in {"api_call", "error_class"}:
            where = (
                f"String reference (e.g. a mock.patch target) to openai through module "
                f"{prefix}; in 1.x patch what {prefix} uses instead (its client, or "
                f"{prefix}.openai.OpenAI): "
                if prefix
                else "String reference (e.g. a mock.patch target): "
            )
            self._report(node, rule.category, where + rule.suggestion, LIKELY)

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
        config = {
            id(t): attr
            for t in node.targets
            if (attr := config_target(t, self.aliases)) in rules.CONFIG_ASSIGN_ATTRS
        }
        if config:  # reported on the whole statement, with a suggestion for the value
            for t in node.targets:
                if id(t) in config:
                    self._report_rule(node, self._config_rule(config[id(t)], node.value))
                else:
                    self.visit(t)
            self.visit(node.value)
        else:
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
