"""AST checks for openai 0.x usage in one Python file.

Every name is resolved through the file's imports to a canonical dotted path
(``oa.ChatCompletion.create`` -> ``openai.ChatCompletion.create``), so only things that really
come from ``openai`` are reported. Limitations are listed in docs/notes.md.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from dataclasses import dataclass

from apifix.engine.migrations.openai_v1 import rules
from apifix.engine.migrations.openai_v1.streaming import content_checks, dict_guarded
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
    visitor = _Visitor(file, source, aliases, _targets_azure(tree, aliases), repo_modules, tree)
    visitor.visit(tree)
    if visitor.findings:  # 0.x code: a lone APIError in a catch is 0.x's too
        for node, suggestion in visitor.catch_apierror:
            visitor._report(node, "error_class", suggestion, LIKELY)
    if aliases:  # key checks on a stream delta: only in files that use openai
        for c in content_checks(tree, source):
            message = rules.STREAM_CONTENT_CHECK.format(check=c.check, delta=c.delta)
            visitor._report(c.node, "response_access", message, LIKELY)
    return visitor.findings


def stream_check_errors(path: str, original: str, candidate: str) -> list[str]:
    """The `streaming` rewrite check: each key check on a stream delta in the rewritten file
    (`hasattr(delta, "content")`, `"content" in delta`; streaming.content_checks), except those
    the original has too in a file that does not use openai (the scanner reports none there, e.g.
    another SDK's stream). Lines are the candidate's."""
    if not path.endswith(".py"):
        return []
    try:
        old, new = ast.parse(original), ast.parse(candidate)
    except SyntaxError:
        return []
    kept: Counter[str] = Counter()
    if not _collect_aliases(old):
        kept.update(c.check for c in content_checks(old, original))
    errors = []
    for c in content_checks(new, candidate):
        if kept[c.check] > 0:
            kept[c.check] -= 1
            continue
        message = rules.STREAM_CONTENT_CHECK.format(check=c.check, delta=c.delta)
        errors.append(f"line {c.line}: {message}")
    return errors


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
        return _Rule("error_class", rules.ERROR_MODULE_GONE, LIKELY)
    new, note, confidence = rules.ERROR_CLASSES[name]
    if new is None:
        return _Rule("error_class", note, confidence)
    suggestion = f"Use openai.{new}" + (f"; {note}" if note else "")
    return _Rule("error_class", suggestion, confidence, name_changes=new != name)


def _error_name(canonical: str | None) -> str | None:
    """`APIError` for openai.APIError or openai.error.APIError; None if not an openai error."""
    parts = (canonical or "").split(".")
    if parts[0] != "openai" or not (len(parts) == 2 or (len(parts) == 3 and parts[1] == "error")):
        return None
    return parts[-1] if parts[-1] in rules.ERROR_NAMES else None


def _classify(canonical: str, azure: bool) -> _Rule | None:
    parts = canonical.split(".")
    if parts[0] != "openai" or len(parts) < 2:
        return None
    head = parts[1]
    if head in rules.RESOURCES or head in rules.REMOVED_RESOURCES:
        return _resource_rule(head, parts[2] if len(parts) > 2 else None, azure)
    if head == "error":
        if len(parts) == 2:
            return _Rule("error_class", rules.ERROR_MODULE_GONE, CERTAIN)
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


def _chain_parts(node: ast.AST) -> tuple[ast.AST, list[ast.Attribute | ast.Subscript]]:
    """(root, the chain's attributes and subscripts from the root outwards)."""
    parts: list[ast.Attribute | ast.Subscript] = []
    while isinstance(node, ast.Attribute | ast.Subscript):
        parts.append(node)
        node = node.value
    return node, parts[::-1]


def _part(node: ast.Attribute | ast.Subscript) -> tuple[str, str | None]:
    """("attr", name), ("key", string key) or ("index", None) for any other subscript."""
    if isinstance(node, ast.Attribute):
        return "attr", node.attr
    s = node.slice
    if isinstance(s, ast.Constant) and isinstance(s.value, str):
        return "key", s.value
    return "index", None


def _mixed_end(parts: list[ast.Attribute | ast.Subscript]) -> tuple[int, bool] | None:
    """(index of the last part, mixed?) of a 0.x response access in `parts` (see
    rules.MIXED_CONTAINERS): mixed, `.choices[i]` then a string key, `["choices"][i]` then an
    attribute, `.usage["total_tokens"]`, `["usage"].total_tokens`; or keys only,
    `["choices"][i]["message"]` / `["data"][i]["embedding"]` (rules.KEY_ONLY_KEYS). None if
    there is none."""
    kinds = [_part(p) for p in parts]
    for i, (kind, name) in enumerate(kinds):
        if kind == "index" or name not in rules.MIXED_CONTAINERS:
            continue
        indexed, keys = rules.MIXED_CONTAINERS[name]  # type: ignore[index]
        start = i + 1
        if indexed:
            if start >= len(kinds) or kinds[start][0] != "index":
                continue
            start += 1
        other = "key" if kind == "attr" else "attr"  # the access style that mixes with it
        end, mixed, strong = None, False, False
        for j in range(start, len(kinds)):
            k, n = kinds[j]
            if k != "index" and n not in keys:
                break
            mixed = mixed or k == other
            strong = strong or (j == start and n in rules.KEY_ONLY_KEYS.get(name, ()))  # type: ignore[arg-type]
            end = j
        if end is not None and (mixed or (kind == "key" and strong)):
            return end, mixed
    return None


def _attribute_form(root: str, parts: list[ast.Attribute | ast.Subscript]) -> str:
    """The chain with identifier string keys written as attributes."""
    out = root
    for p in parts:
        kind, name = _part(p)
        if kind == "attr" or (kind == "key" and name and name.isidentifier()):
            out += f".{name}"
        else:
            out += f"[{ast.unparse(p.slice)}]"  # type: ignore[union-attr]
    return out


def _namespace(node: ast.expr, source: str, depth: int = 0) -> str | None:
    """A dict/list literal as SimpleNamespace objects (`{"text": "x"}` ->
    `SimpleNamespace(text="x")`, other values as written); None when a key is no identifier
    or it gets too deep."""
    if depth > 4:
        return None
    if isinstance(node, ast.Dict):
        items = []
        for k, v in zip(node.keys, node.values, strict=True):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return None
            if not k.value.isidentifier():
                return None
            value = _namespace(v, source, depth + 1)
            if value is None:
                return None
            items.append(f"{k.value}={value}")
        return f"SimpleNamespace({', '.join(items)})"
    if isinstance(node, ast.List | ast.Tuple):
        elts = [_namespace(e, source, depth + 1) for e in node.elts]
        if any(e is None for e in elts):
            return None
        inner = ", ".join(elts)  # type: ignore[arg-type]
        return f"[{inner}]" if isinstance(node, ast.List) else f"({inner}{',' * (len(elts) == 1)})"
    return " ".join((ast.get_source_segment(source, node) or ast.unparse(node)).split())


# --- 1.x clients ----------------------------------------------------------------------------

_Scopes = tuple[int, ...]  # ids of the enclosing function scopes, innermost last (module first)


class _Clients:
    """Names and `self.x` attributes bound to a 1.x client (a constructor call such as
    `AzureOpenAI(...)`, or `.with_options(...)` of a client), flow-insensitive: names per
    function scope (module included), attributes per class (any method's `self.x = ...` / `cls.x`,
    or `x = ...` in the class body)."""

    def __init__(self, tree: ast.Module, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.names: dict[int, set[str]] = {}
        self.attrs: dict[int, set[str]] = {}
        if not aliases:
            return
        for _ in range(3):  # a client copied from a client bound later: a few rounds suffice
            before = sum(map(len, self.names.values())) + sum(map(len, self.attrs.values()))
            self._collect(tree.body, (id(tree),), None, in_class=False)
            after = sum(map(len, self.names.values())) + sum(map(len, self.attrs.values()))
            if after == before:
                break

    def _collect(
        self, body: list[ast.stmt], scopes: _Scopes, cls: int | None, *, in_class: bool
    ) -> None:
        for stmt in body:
            for node in _scope_nodes(stmt):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    self._collect(node.body, (*scopes, id(node)), cls, in_class=False)
                elif isinstance(node, ast.ClassDef):
                    self._collect(node.body, scopes, id(node), in_class=True)
                else:
                    self._bind_node(node, scopes, cls, in_class)

    def _bind_node(self, node: ast.AST, scopes: _Scopes, cls: int | None, in_class: bool) -> None:
        pairs: list[tuple[ast.AST, ast.AST | None]] = []
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
            pairs = [(node.target, node.value)]
        elif isinstance(node, ast.With | ast.AsyncWith):
            pairs = [(i.optional_vars, i.context_expr) for i in node.items if i.optional_vars]
        for target, value in pairs:
            if value is None or not self.is_client(value, scopes, cls):
                continue
            if isinstance(target, ast.Name):
                if in_class and cls is not None:
                    self.attrs.setdefault(cls, set()).add(target.id)
                else:
                    self.names.setdefault(scopes[-1], set()).add(target.id)
            elif (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in ("self", "cls")
                and cls is not None
            ):
                self.attrs.setdefault(cls, set()).add(target.attr)

    def is_client(self, node: ast.AST, scopes: _Scopes, cls: int | None) -> bool:
        if isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Call):
            func = node.func
            canonical = _resolve(func, self.aliases)
            if canonical:
                parts = canonical.split(".")
                return len(parts) == 2 and parts[1] in rules.CLIENT_CONSTRUCTORS
            return (
                isinstance(func, ast.Attribute)
                and func.attr in rules.CLIENT_COPIES
                and self.is_client(func.value, scopes, cls)
            )
        if isinstance(node, ast.Name):
            return any(node.id in self.names.get(s, ()) for s in scopes)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return (
                node.value.id in ("self", "cls")
                and cls is not None
                and node.attr in self.attrs.get(cls, ())
            )
        return False

    def method(self, node: ast.AST, scopes: _Scopes, cls: int | None) -> bool:
        """`node` (not called) is an openai method: a 0.x resource method
        (`openai.Completion.create`) or a 1.x one, `openai.<resource>...<method>` (the
        module-level client) or `<client>.<resource>...<method>`."""
        if not isinstance(node, ast.Attribute):
            return False
        canonical = _resolve(node, self.aliases)
        if canonical:
            parts = canonical.split(".")
            if parts[0] != "openai":
                return False
            if len(parts) == 3 and parts[1] in rules.RESOURCES:
                return True
            return len(parts) >= 3 and parts[1] in rules.CLIENT_RESOURCES
        names: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            names.append(cur.attr)
            if (
                len(names) >= 2
                and cur.attr in rules.CLIENT_RESOURCES
                and self.is_client(cur.value, scopes, cls)
            ):
                return True
            cur = cur.value
        return False


def _scope_nodes(stmt: ast.stmt):
    """The nodes of `stmt` in its own scope: nested function and class definitions are yielded
    but not entered (lambdas and comprehensions are entered: `:=` binds in the enclosing
    scope)."""
    stack: list[ast.AST] = [stmt]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _top_level_params(tree: ast.Module | None) -> dict[str, frozenset[str]]:
    """Explicit parameter names of each top-level function of the file."""
    out: dict[str, frozenset[str]] = {}
    for node in tree.body if tree else ():
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            a = node.args
            out[node.name] = frozenset(p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs))
    return out


# --- visitor --------------------------------------------------------------------------------


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        file: str,
        source: str,
        aliases: dict[str, str],
        azure: bool,
        repo_modules: frozenset[str] = frozenset(),
        tree: ast.Module | None = None,
    ) -> None:
        self.file = file
        self.tree = tree
        self._parents: dict[int, ast.AST] | None = None
        self.repo_modules = repo_modules
        self.source = source
        self.aliases = aliases
        self.azure = azure
        self.test_file = is_test_file(file, source)
        self.findings: list[Finding] = []
        # One dict per function/class scope: name -> True if it holds a stream, else False.
        self.scopes: list[dict[str, bool]] = [{}]
        # One set per scope: names last assigned a plain dict (_dict_value).
        self.dict_names: list[set[str]] = [set()]
        self.streaming_calls: set[int] = set()  # ids of the func of resource calls with stream=
        self.clients = _Clients(tree, aliases) if tree is not None else None
        self.fn_scopes: list[int] = [id(tree)]  # _Scopes of the node being visited
        self.classes: list[int | None] = [None]
        self.local_params = _top_level_params(tree)
        # APIError in a catch in the form 1.x also uses: reported if the file has other findings.
        self.catch_apierror: list[tuple[ast.AST, str]] = []
        self.catch_reported: set[int] = set()  # openai.error.APIError nodes reported by _catch

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
        if id(node) in self.catch_reported:
            return
        canonical = _resolve(node, self.aliases)
        if canonical and (rule := _classify(canonical, self.azure)):
            attr = canonical.split(".", 1)[1]
            if attr in ("api_key", "organization") and not self.azure:  # a read
                rule = _Rule(rule.category, rules.CONFIG_READ.format(attr=attr), rule.confidence)
            if id(node) in self.streaming_calls:
                rule = _Rule(rule.category, rule.suggestion + rules.STREAM_CALL, rule.confidence)
            self._report_rule(node, rule)
            return  # the rest of the chain is part of this finding
        if self._mixed_access(node):
            return
        self.generic_visit(node)

    def _mixed_access(self, node: ast.Attribute | ast.Subscript) -> bool:
        """Report a 0.x access in the chain ending at `node` by its shape (_mixed_end), in a file
        that imports openai, unless its root is a response this file tracks (visit_Subscript
        reports those) or, for keys only, a plain dict (_dict_root). True when reported; the
        chain's root and subscript keys are then visited, not its inner parts."""
        if not self.aliases:
            return False
        root, parts = _chain_parts(node)
        found = _mixed_end(parts)
        if found is None or self._response_kind(root) is not None:
            return False
        end, mixed = found
        if not mixed and self._dict_root(root):
            return False
        segment = ast.get_source_segment(self.source, root) or ast.unparse(root)
        new = _attribute_form(" ".join(segment.split()), parts[: end + 1])
        if len(new) > _MAX_PATTERN:
            new = _attribute_form("resp", parts[: end + 1])
        message = (rules.MIXED_ACCESS if mixed else rules.KEY_ACCESS).format(new=new)
        self._report(parts[end], "response_access", message, LIKELY)
        self.visit(root)
        for p in parts:
            if isinstance(p, ast.Subscript):
                self.visit(p.slice)
        return True

    @staticmethod
    def _dict_value(node: ast.AST | None) -> bool:
        """Whether `node` evaluates to a plain dict: a dict literal or comprehension,
        `dict(...)`, `json.load(s)(...)`, `x.model_dump()` / `to_dict()` / `json()`
        (rules.DICT_RESULT_METHODS)."""
        if isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Dict | ast.DictComp):
            return True
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "dict"
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id == "json":
                return func.attr in {"load", "loads"}
            return func.attr in rules.DICT_RESULT_METHODS
        return False

    def _dict_root(self, root: ast.AST) -> bool:
        """`root` is a plain dict: a dict value, a name last assigned one in this scope, or a
        name under a dict guard (`if isinstance(chunk, dict):`, streaming.dict_guarded)."""
        if self._dict_value(root):
            return True
        if not isinstance(root, ast.Name):
            return False
        if root.id in self.dict_names[-1]:
            return True
        if self.tree is None:
            return False
        if self._parents is None:
            self._parents = {id(c): p for p in ast.walk(self.tree) for c in ast.iter_child_nodes(p)}
        return dict_guarded(root, root.id, self._parents)

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

    # APIError in an except type or an exception tuple (retry lists): a sibling of RateLimitError
    # etc. in 0.x, the base of every API error in 1.x

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None and not isinstance(node.type, ast.Tuple):
            self._catch([node.type])
        self.generic_visit(node)

    def _visit_literal(self, node: ast.Tuple | ast.List | ast.Set) -> None:
        self._catch(node.elts)
        self.generic_visit(node)

    visit_Tuple = _visit_literal
    visit_List = _visit_literal
    visit_Set = _visit_literal

    def _catch(self, elts: list[ast.expr]) -> None:
        if self.test_file:  # tests list exceptions to raise, not to catch
            return
        errors = [(e, name) for e in elts if (name := _error_name(_resolve(e, self.aliases)))]
        for node, name in errors:
            if name != "APIError":
                continue
            server = next((e for e, n in errors if n in rules.SERVER_ERRORS), None)
            if server is None:
                suggestion = rules.CATCH_APIERROR
            else:
                present = ast.get_source_segment(self.source, server) or ""
                if _error_name(_resolve(server, self.aliases)) == "ServiceUnavailableError":
                    present += " (1.x openai.InternalServerError, every 5xx)"
                else:
                    present += " (every 5xx)"
                suggestion = rules.CATCH_APIERROR_DROP.format(present=present)
            old = _resolve(node, self.aliases) == "openai.error.APIError"
            if old and isinstance(node, ast.Attribute):  # the path itself is certain to change
                self.catch_reported.add(id(node))
                self._report(
                    node, "error_class", f"{rules.ERROR_MODULE_GONE}; {suggestion}", CERTAIN
                )
            elif old or any(n not in ("APIError", "OpenAIError") for _, n in errors):
                self._report(node, "error_class", suggestion, LIKELY)
            else:
                self.catch_apierror.append((node, suggestion))

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
    def _may_stream(call: ast.Call) -> bool:
        """`stream=` with anything but False / None (`stream=stream` may stream)."""
        return any(
            kw.arg == "stream"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value in (False, None))
            for kw in call.keywords
        )

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
        if call is not None and self._may_stream(call):
            self.streaming_calls.add(id(call.func))
        self._per_call_kwargs(node)
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
        if self.test_file:
            self._mock_keyword_data(node)
        self.generic_visit(node)

    def _is_method(self, node: ast.AST) -> bool:
        if self.clients is None:
            return False
        return self.clients.method(node, tuple(self.fn_scopes), self.classes[-1])

    def _per_call_kwargs(self, node: ast.Call) -> None:
        """Keywords 0.x methods took per call (rules.KWARG_CHANGES) on a call of an openai method
        (0.x or 1.x), or on a wrapper call passed one (`call_with_retry(func=client.completions.
        create, engine=...)`, the kwargs go through), unless the wrapper is a top-level function
        of this file with that parameter of its own."""
        wrapper = method = None
        if not self._is_method(node.func):
            args = [*node.args, *(kw.value for kw in node.keywords)]
            method = next((a for a in args if self._is_method(a)), None)
            if method is None:
                return
            wrapper = " ".join(ast.unparse(node.func).split())
        own = (
            self.local_params.get(node.func.id, frozenset())
            if wrapper and isinstance(node.func, ast.Name)
            else frozenset()
        )
        model = next((kw.value for kw in node.keywords if kw.arg == "model"), None)
        for kw in node.keywords:
            if kw.arg not in rules.KWARG_CHANGES or kw.arg in own:
                continue
            message = rules.KWARG_CHANGES[kw.arg].format(value=self._value_text(kw.value))
            if kw.arg in ("engine", "deployment_id") and model is not None:
                both = rules.KWARG_MODEL_AZURE if self.azure else rules.KWARG_MODEL_TOO
                message += both.format(
                    model=self._value_text(model), value=self._value_text(kw.value)
                )
            if method is not None:
                via = rules.KWARG_VIA.format(
                    kwarg=kw.arg, wrapper=wrapper, method=" ".join(ast.unparse(method).split())
                )
                message = via + message
            self._report(kw, "api_call", message, CERTAIN)

    def _value_text(self, node: ast.AST) -> str:
        text = " ".join((ast.get_source_segment(self.source, node) or ast.unparse(node)).split())
        return text if len(text) <= _MAX_VALUE else "..."

    def visit_Subscript(self, node: ast.Subscript) -> None:
        cur, chain, has_key = _unwrap_chain(node)
        kind = self._response_kind(cur) if has_key else None
        if kind is None:
            if not self._mixed_access(node):
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

    def _bind(self, target: ast.AST, kind: bool | None, value: ast.AST | None = None) -> None:
        if isinstance(target, ast.Name):
            if self._dict_value(value):
                self.dict_names[-1].add(target.id)
            else:
                self.dict_names[-1].discard(target.id)
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
            if self.test_file:
                self._mock_attr_data(node)
            self.generic_visit(node)
        kind = self._response_kind(node.value)
        for t in node.targets:
            self._bind(t, kind, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.generic_visit(node)
        if node.value is not None:
            self._bind(node.target, self._response_kind(node.value), node.value)

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

    def _response_dict(self, node: ast.Dict) -> str | None:
        """The suggestion for {"choices": [{"message": ...}]} or {"choices": [{"text": ...}]}
        (a response), {"data": [{"embedding": ...}]} (embeddings), or
        {"choices": [{"delta": ...}]} (a stream chunk); list or tuple."""
        for key, value in zip(node.keys, node.values, strict=True):
            name = self._key(key)
            if name not in (rules.RESPONSE_DICT_KEY, rules.EMBEDDINGS_DICT_KEY):
                continue
            if isinstance(value, ast.List | ast.Tuple):
                for elt in value.elts:
                    if not isinstance(elt, ast.Dict):
                        continue
                    keys = {self._key(k) for k in elt.keys}
                    if name == rules.EMBEDDINGS_DICT_KEY:
                        if rules.EMBEDDING_ITEM_KEY in keys:
                            return rules.MOCK_DICT_RESPONSE
                        continue
                    if keys & rules.RESPONSE_CHOICE_KEYS:
                        return rules.MOCK_DICT_RESPONSE
                    if rules.CHUNK_CHOICE_KEY in keys:
                        return self._chunk_suggestion(elt)
        return None

    def _chunk_suggestion(self, choice: ast.Dict) -> str:
        """MOCK_DICT_CHUNK, or MOCK_DICT_CHUNK_ROLE with the role when the delta has one (its
        literal value, else `...`)."""
        delta = next(
            (
                v
                for k, v in zip(choice.keys, choice.values, strict=True)
                if self._key(k) == rules.CHUNK_CHOICE_KEY
            ),
            None,
        )
        if isinstance(delta, ast.Dict):
            for key, value in zip(delta.keys, delta.values, strict=True):
                if self._key(key) == "role":
                    role = self._key(value)
                    shown = json.dumps(role) if isinstance(role, str) else "..."
                    return rules.MOCK_DICT_CHUNK_ROLE.format(role=shown)
        return rules.MOCK_DICT_CHUNK

    def visit_Dict(self, node: ast.Dict) -> None:
        suggestion = self._response_dict(node) if self.test_file else None
        if suggestion:
            self._report(node, "response_access", suggestion, LIKELY)
        self.generic_visit(node)

    @staticmethod
    def _mock_attr_shaped(attr: str, value: ast.expr, chain: ast.expr | None = None) -> bool:
        """Whether `value`, put on attribute (or keyword) `attr`, is dict-shaped 0.x response
        data: choices / data as a list of dicts with a choice / item key, usage as a dict with
        a token key, and (`chain`: what the attribute is read from) a choice's message /
        delta as a dict with a message key when `chain` reads `.choices[...]`."""

        def keys(d: ast.Dict) -> set[object]:
            return {k.value for k in d.keys if isinstance(k, ast.Constant)}

        if attr in rules.MOCK_ATTR_KEYS:
            wanted = rules.MOCK_ATTR_KEYS[attr]
            return isinstance(value, ast.List | ast.Tuple) and any(
                isinstance(e, ast.Dict) and keys(e) & wanted for e in value.elts
            )
        if attr == "usage":
            return isinstance(value, ast.Dict) and bool(keys(value) & rules.USAGE_KEYS)
        if attr in rules.MOCK_MESSAGE_ATTRS and chain is not None:
            _, parts = _chain_parts(chain)
            reads_choice = any(
                _part(p) == ("attr", "choices")
                and i + 1 < len(parts)
                and _part(parts[i + 1])[0] == "index"
                for i, p in enumerate(parts)
            )
            return (
                reads_choice
                and isinstance(value, ast.Dict)
                and bool(keys(value) & rules.MOCK_MESSAGE_KEYS)
            )
        return False

    def _mock_attr_report(self, node: ast.AST, target: str, value: ast.expr) -> None:
        example = _namespace(value, self.source)
        if example is None or len(target) + len(example) > _MAX_PATTERN:
            example = "SimpleNamespace(...) objects"
            target = ""
        message = rules.MOCK_ATTR_DATA.format(target=target, example=example)
        self._report(node, "response_access", message, LIKELY)

    def _mock_attr_data(self, node: ast.Assign) -> None:
        """Test files: dict-shaped mock data assigned to a response attribute
        (`m.choices = [{"text": "x"}]`, `m.return_value.data = [...]`)."""
        for t in node.targets:
            if isinstance(t, ast.Attribute) and self._mock_attr_shaped(t.attr, node.value, t.value):
                segment = ast.get_source_segment(self.source, t) or ast.unparse(t)
                self._mock_attr_report(node, f"{' '.join(segment.split())} = ", node.value)
                return

    def _mock_keyword_data(self, node: ast.Call) -> None:
        """Test files: dict-shaped mock data passed as `choices=` / `data=` / `usage=`
        (`MagicMock(choices=[{"text": "x"}])`, `dict(choices=...)`)."""
        for kw in node.keywords:
            if kw.arg and self._mock_attr_shaped(kw.arg, kw.value):
                self._mock_attr_report(kw, f"{kw.arg}=", kw.value)

    def _visit_scope(self, node: ast.AST) -> None:
        self.scopes.append({})
        self.dict_names.append(set())
        function = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        if function:
            self.fn_scopes.append(id(node))
        if isinstance(node, ast.ClassDef):
            self.classes.append(id(node))
        self.generic_visit(node)
        if function:
            self.fn_scopes.pop()
        if isinstance(node, ast.ClassDef):
            self.classes.pop()
        self.scopes.pop()
        self.dict_names.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope
    visit_Lambda = _visit_scope
