from __future__ import annotations

import ast

from apifix.engine.api_names import in_modules, module_aliases, resolve

GOOGLE = frozenset({"google.generativeai", "google.genai"})


def test_in_modules_matches_by_dotted_prefix():
    assert in_modules("openai", {"openai"})
    assert in_modules("openai.types.chat", {"openai"})
    assert not in_modules("openai_helpers", {"openai"})
    assert in_modules("google.genai.types", GOOGLE)
    assert in_modules("google.generativeai", GOOGLE)
    assert not in_modules("google", GOOGLE)
    assert not in_modules("google.cloud.storage", GOOGLE)
    assert not in_modules("google.genaix", GOOGLE)


def test_module_aliases_keeps_only_bindings_of_library_modules():
    tree = ast.parse(
        "import google.generativeai as genai\n"
        "import google.genai.types as gt\n"
        "import google.protobuf as pb\n"
        "import google.genai\n"
        "from google import genai as new_genai\n"
        "from google.genai import types\n"
        "from openai import OpenAI\n"
        "from . import genai as local\n"
    )
    assert module_aliases(tree, GOOGLE) == {
        "genai": "google.generativeai",
        "gt": "google.genai.types",
        "new_genai": "google.genai",
    }
    assert module_aliases(tree, {"openai"}) == {}
    assert module_aliases(ast.parse("import openai as oa"), {"openai"}) == {"oa": "openai"}


def test_resolve_replaces_the_first_part():
    aliases = {"genai": "google.genai"}
    assert resolve("genai.Client", aliases) == "google.genai.Client"
    assert resolve("genai", aliases) == "google.genai"
    assert resolve("google.genai.Client", aliases) == "google.genai.Client"
    assert resolve("other.genai", aliases) == "other.genai"
