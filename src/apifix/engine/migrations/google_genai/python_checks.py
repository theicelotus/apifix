"""AST checks for google-generativeai (the old Gemini SDK) usage in one Python file.

Names are resolved through the file's imports to a canonical dotted path under
``google.generativeai`` (``gg.GenerativeModel`` -> ``google.generativeai.GenerativeModel``), the
same way `openai_v1.python_checks` does for openai. Instance tracking (which names hold a
`GenerativeModel` or a `ChatSession`) is whole-file and flow-insensitive, simpler than openai's
per-scope tracking: a model name bound in one function is treated as a model everywhere else in
the file too. This can over-associate in a file that reuses a name for two unrelated things, but
google-generativeai code rarely does; known limitations are listed in docs/notes.md.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from apifix.engine.migrations.google_genai import rules
from apifix.engine.models import CERTAIN, LIKELY, Category, Confidence, Finding
from apifix.engine.project import is_test_file

_MAX_PATTERN = 120
_MAX_VALUE = 60
_OLD_ROOT = "google.generativeai"
_NEW_ROOT = "google.genai"
_API_CORE_ROOT = "google.api_core"


@dataclass(frozen=True)
class _Rule:
    category: Category
    suggestion: str
    confidence: Confidence


def _in_root(dotted: str, root: str) -> bool:
    return dotted == root or dotted.startswith(f"{root}.")


def _collect_aliases(tree: ast.Module, root: str) -> dict[str, str]:
    """Local name -> canonical dotted path, for every name an import binds under `root`
    (``google.generativeai`` or ``google.genai``): a module alias (bare, ``as x``, ``from
    google import generativeai as gg``) or a name imported directly from inside it."""
    aliases: dict[str, str] = {}
    top = root.split(".", 1)[0]  # "google"
    rest = root.split(".", 1)[1]  # "generativeai" / "genai"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if not _in_root(a.name, root):
                    continue
                if a.asname:
                    aliases[a.asname] = a.name
                else:  # `import google.generativeai[.types]` binds `google`
                    aliases[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module == top:
                for a in node.names:
                    if a.name == rest:
                        aliases[a.asname or a.name] = root
            elif _in_root(module, root):
                for a in node.names:
                    if a.name != "*":
                        aliases[a.asname or a.name] = f"{module}.{a.name}"
    return aliases


def _resolve(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, aliases)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_old_sdk_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(_in_root(a.name, _OLD_ROOT) for a in node.names)
    if node.level:
        return False
    module = node.module or ""
    if module == "google":
        return any(a.name == "generativeai" for a in node.names)
    return _in_root(module, _OLD_ROOT)


def _is_api_core_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(_in_root(a.name, _API_CORE_ROOT) for a in node.names)
    if node.level:
        return False
    module = node.module or ""
    if module == "google":
        return any(a.name == "api_core" for a in node.names)
    return _in_root(module, _API_CORE_ROOT)


def _module_rule(suffix: str) -> _Rule | None:
    """`suffix`: the canonical name with the `google.generativeai.` prefix stripped (e.g.
    ``configure``, ``types.GenerationConfig``)."""
    name = suffix.rsplit(".", 1)[-1]
    if suffix.startswith("types.") or suffix == "types":
        if name in rules.ERROR_CLASSES:
            return _Rule("error_class", rules.ERROR_CLASSES[name], CERTAIN)
        if name == "GenerationConfig":
            cat, text, conf = rules.MODULE_CALLS["GenerationConfig"]
            return _Rule(cat, text, conf)
        return None
    if suffix == "protos" or suffix.startswith("protos."):
        return _Rule("removed_module", rules.PROTOS_MODULE, CERTAIN)
    if name in rules.MODULE_CALLS:
        cat, text, conf = rules.MODULE_CALLS[name]
        return _Rule(cat, text, conf)
    return None


def _value_text(source: str, node: ast.AST) -> str:
    text = " ".join((ast.get_source_segment(source, node) or ast.unparse(node)).split())
    return text if len(text) <= _MAX_VALUE else "..."


def _is_true_stream(call: ast.Call) -> bool:
    return any(
        kw.arg == "stream" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in call.keywords
    )


def _may_stream(call: ast.Call) -> bool:
    return any(
        kw.arg == "stream"
        and not (isinstance(kw.value, ast.Constant) and kw.value.value in (False, None))
        for kw in call.keywords
    )


class _Instances:
    """Names and `self.<attr>` bound to a `GenerativeModel` or a `ChatSession`, whole-file and
    flow-insensitive (module docstring)."""

    def __init__(self, tree: ast.Module, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.model_names: set[str] = set()
        self.model_attrs: set[str] = set()
        self.chat_names: set[str] = set()
        self.chat_attrs: set[str] = set()
        if not aliases:
            return
        for _ in range(3):
            before = self._count()
            for node in ast.walk(tree):
                self._bind(node)
            if self._count() == before:
                break

    def _count(self) -> int:
        return (
            len(self.model_names)
            + len(self.model_attrs)
            + len(self.chat_names)
            + len(self.chat_attrs)
        )

    def _bind(self, node: ast.AST) -> None:
        pairs: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            pairs = [(node.target, node.value)]
        for target, value in pairs:
            is_model = self.is_model(value)
            is_chat = self.is_chat(value)
            if not (is_model or is_chat):
                continue
            if isinstance(target, ast.Name):
                (self.model_names if is_model else self.chat_names).add(target.id)
            elif (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in ("self", "cls")
            ):
                (self.model_attrs if is_model else self.chat_attrs).add(target.attr)

    def is_model(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Call):
            canonical = _resolve(node.func, self.aliases)
            return canonical == f"{_OLD_ROOT}.GenerativeModel"
        if isinstance(node, ast.Name):
            return node.id in self.model_names
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return node.value.id in ("self", "cls") and node.attr in self.model_attrs
        return False

    def is_chat(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Await):
            node = node.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return node.func.attr == "start_chat" and self.is_model(node.func.value)
        if isinstance(node, ast.Name):
            return node.id in self.chat_names
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return node.value.id in ("self", "cls") and node.attr in self.chat_attrs
        return False


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        file: str,
        source: str,
        aliases: dict[str, str],
        instances: _Instances,
        tree: ast.Module,
    ) -> None:
        self.file = file
        self.source = source
        self.aliases = aliases
        self.instances = instances
        self.test_file = is_test_file(file, source)
        self.findings: list[Finding] = []
        # names bound from embed_content(...) / embed_content_async(...): dict-style access on
        # them is CERTAIN rather than LIKELY.
        self.embedding_names: set[str] = set()
        self.api_core_aliases = _collect_aliases(tree, _API_CORE_ROOT)
        self.new_aliases = _collect_aliases(tree, _NEW_ROOT)

    def _report(
        self, node: ast.AST, category: Category, suggestion: str, confidence: Confidence
    ) -> None:
        segment = " ".join((ast.get_source_segment(self.source, node) or "").split())
        if len(segment) > _MAX_PATTERN:
            segment = segment[: _MAX_PATTERN - 3] + "..."
        self.findings.append(
            Finding(
                file=self.file,
                line=node.lineno,
                col=node.col_offset + 1,
                end_line=node.end_lineno or node.lineno,
                end_col=(node.end_col_offset or node.col_offset) + 1,
                category=category,
                old_pattern=segment,
                suggestion=suggestion,
                confidence=confidence,
            )
        )

    def _report_rule(self, node: ast.AST, rule: _Rule) -> None:
        self._report(node, rule.category, rule.suggestion, rule.confidence)

    # imports

    def visit_Import(self, node: ast.Import) -> None:
        if _is_old_sdk_import(node):
            self._report(node, "import", rules.KNOWN_IMPORTS["genai"], CERTAIN)
        elif _is_api_core_import(node):
            self._report(node, "import", rules.API_CORE_IMPORT, LIKELY)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if _is_old_sdk_import(node):
            self._report(node, "import", rules.KNOWN_IMPORTS["genai"], CERTAIN)
            return  # the names themselves are not separately reported as imports
        if _is_api_core_import(node):
            self._report(node, "import", rules.API_CORE_IMPORT, LIKELY)
        self.generic_visit(node)

    # calls

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        canonical = _resolve(func, self.aliases)
        reported = False
        if canonical and _in_root(canonical, _OLD_ROOT):
            reported = self._module_call(node, canonical)
        elif isinstance(func, ast.Attribute):
            reported = self._method_call(node, func)
        if not reported:
            self.visit(func)
        if self.test_file:
            self._patch_target(node)
        for a in node.args:
            self.visit(a)
        for kw in node.keywords:
            self.visit(kw.value)

    def _patch_target(self, node: ast.Call) -> None:
        """A string patch target in a test (`patch("pkg.mod.genai.GenerativeModel")`,
        `monkeypatch.setattr("google.generativeai.configure", ...)`, `patch.object(genai,
        "configure")`) that names something the new SDK's module does not have. Reported on
        the string, whether or not the test imports the old SDK."""
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in ("patch", "setattr", "object") or not node.args:
            return
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            self._report_target(first, first.value)
            return
        second = node.args[1] if len(node.args) > 1 else None
        if name == "object" and isinstance(second, ast.Constant) and isinstance(second.value, str):
            module = _resolve(first, self.aliases) or _resolve(first, self.new_aliases)
            if module is None:
                text = ast.unparse(first)  # `patch.object(llms.genai, "configure")`
                module = text if text.rsplit(".", 1)[-1] == "genai" else None
            if module:
                self._report_target(second, f"{module}.{second.value}")

    def _report_target(self, node: ast.Constant, target: str) -> None:
        parts = target.split(".")
        if len(parts) < 2 or not all(p.isidentifier() for p in parts):
            return
        if target.startswith(f"{_OLD_ROOT}."):
            prefix = _NEW_ROOT
            suffix = target[len(_OLD_ROOT) + 1 :]
            name = suffix.split(".")[0]
            rule = _module_rule(suffix) or _module_rule(name)
        else:
            at = next(
                (
                    i
                    for i, p in enumerate(parts[:-1])
                    if p == "genai" and parts[i + 1] in rules.MODULE_CALLS
                ),
                None,
            )
            if at is None:
                return
            prefix = ".".join(parts[: at + 1])
            name = parts[at + 1]
            rule = _module_rule(name)
        if rule is None:
            return
        text = rules.PATCH_TARGET.format(
            target=target, name=name, prefix=prefix, what=rule.suggestion
        )
        self._report(node, rule.category, text, LIKELY)

    def _module_call(self, node: ast.Call, canonical: str) -> bool:
        suffix = canonical[len(_OLD_ROOT) + 1 :]
        rule = _module_rule(suffix)
        if rule is None:
            return False
        self._report_rule(node, rule)
        return True

    def _method_call(self, node: ast.Call, func: ast.Attribute) -> bool:
        name = func.attr
        if self.instances.is_model(func.value) and name in rules.MODEL_METHODS:
            cat, text, conf = rules.MODEL_METHODS[name]
            if name == "generate_content" and _may_stream(node):
                text += rules.STREAM_CALL_NOTE
            self._report(node, cat, text, conf)
            return True
        if self.instances.is_chat(func.value) and name in rules.CHAT_METHODS:
            cat, text, conf = rules.CHAT_METHODS[name]
            if name == "send_message" and _may_stream(node):
                text += rules.CHAT_STREAM_CALL_NOTE
            self._report(node, cat, text, conf)
            return True
        if name == "resolve" and self._looks_like_response(func.value):
            self._report(node, "response_access", rules.RESOLVE_REMOVED, LIKELY)
            return True
        if name == "to_dict" and self._looks_like_response(func.value):
            self._report(node, "response_access", rules.TO_DICT_RENAMED, LIKELY)
            return True
        return False

    def _looks_like_response(self, node: ast.AST) -> bool:
        del node  # no per-instance response tracking yet; gated on the file using the old SDK
        return bool(self.aliases) and not self.test_file

    # attributes (chat.history, Model.supported_generation_methods, errors, api_core)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self.instances.is_chat(node.value) and node.attr in rules.CHAT_ATTRS:
            cat, text, conf = rules.CHAT_ATTRS[node.attr]
            self._report(node, cat, text, conf)
            return
        if node.attr == "supported_generation_methods" and bool(self.aliases):
            self._report(node, "response_access", rules.SUPPORTED_ACTIONS_RENAMED, LIKELY)
            return
        canonical = _resolve(node, self.aliases)
        if canonical and _in_root(canonical, _OLD_ROOT):
            suffix = canonical[len(_OLD_ROOT) + 1 :]
            rule = _module_rule(suffix)
            if rule is not None:
                self._report_rule(node, rule)
                return
        if self._api_core_error(node):
            return
        self.generic_visit(node)

    def _api_core_error(self, node: ast.Attribute | ast.Name) -> bool:
        """Report an exception under google.api_core.exceptions (any file: the new SDK does not
        install google-api-core, so a test that raises one fails to import too)."""
        dotted = _resolve(node, self.api_core_aliases)
        prefix = f"{_API_CORE_ROOT}.exceptions."
        if not dotted or not dotted.startswith(prefix):
            return False
        name = dotted[len(prefix) :]
        if "." in name:
            return False
        new, note = rules.API_CORE_ERRORS.get(name, rules.API_CORE_OTHER)
        self._report(node, "error_class", rules.API_CORE_NOTE.format(new=new, note=note), LIKELY)
        return True

    def visit_Name(self, node: ast.Name) -> None:
        if self._api_core_error(node):
            return
        canonical = self.aliases.get(node.id)
        if canonical == f"{_OLD_ROOT}.GenerativeModel":
            return  # reported where constructed (visit_Call), not at every bare reference
        if canonical and _in_root(canonical, _OLD_ROOT):
            suffix = canonical[len(_OLD_ROOT) + 1 :]
            rule = _module_rule(suffix)
            if rule is not None:
                self._report_rule(node, rule)

    # dict-style responses: embedding["embedding"], .text in try/except ValueError

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = node.slice.value if isinstance(node.slice, ast.Constant) else None
        if (
            isinstance(key, str)
            and key == "embedding"
            and isinstance(node.value, ast.Name)
            and bool(self.aliases)
        ):
            certain = node.value.id in self.embedding_names
            self._report(
                node,
                "response_access",
                rules.EMBEDDING_DICT_ACCESS,
                CERTAIN if certain else LIKELY,
            )
            return
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call):
            canonical = _resolve(node.value.func, self.aliases)
            if canonical in (
                f"{_OLD_ROOT}.embed_content",
                f"{_OLD_ROOT}.embed_content_async",
            ):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.embedding_names.add(t.id)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        catches_value_error = any(
            (isinstance(h.type, ast.Name) and h.type.id == "ValueError")
            or (
                isinstance(h.type, ast.Tuple)
                and any(isinstance(e, ast.Name) and e.id == "ValueError" for e in h.type.elts)
            )
            for h in node.handlers
        )
        if catches_value_error and bool(self.aliases):
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(inner, ast.Attribute) and inner.attr == "text":
                    self._report(inner, "response_access", rules.RESPONSE_TEXT_NONE, LIKELY)
        self.generic_visit(node)


def check_python(
    file: str, tree: ast.Module, source: str, repo_modules: frozenset[str] = frozenset()
) -> list[Finding]:
    """`repo_modules` is accepted for interface parity with openai-v1 but not used: this
    migration does not yet resolve string references through the repo's own modules."""
    aliases = _collect_aliases(tree, _OLD_ROOT)
    instances = _Instances(tree, aliases)
    visitor = _Visitor(file, source, aliases, instances, tree)
    visitor.visit(tree)
    return visitor.findings


def new_api_uses(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, name) of every use of the new SDK (`genai.Client`, `google.genai.types`, ...),
    aliases resolved; used by already_migrated.py."""
    aliases = _collect_aliases(tree, _NEW_ROOT)
    found: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _in_root(node.module or "", _NEW_ROOT)
        ):
            found.add((node.lineno, node.module or ""))
        elif isinstance(node, ast.Name | ast.Attribute):
            canonical = _resolve(node, aliases)
            if canonical and _in_root(canonical, _NEW_ROOT):
                found.add((node.lineno, canonical))
    return sorted(found)
