# SDK lookalikes in the unrelated-edit check, patch where it's used

Requested 2026-10-23. The request is verbatim. Built in the same step (no review stop was
asked for).

## Request

Context: Read CLAUDE.md. SuspensePerception run ~/apifix-runs/suspense-gptoss-2/ (verified_weak; the repo’s tests were already broken) has two problems in conftest.py that no check caught:

mock_together (the Together SDK’s mock, unrelated to openai) was rewritten, including a behaviour change (content=mock_response now refers to a different object). The unrelated-edit check probably treated it as related because Together’s client uses the same names as openai 1.x (chat.completions.create, choices, message, content).
mock_openai patches "openai.OpenAI", but the migrated src/thriller/api.py and misc.py use from openai import OpenAI, so the patch never applies to them. It should be thriller.api.OpenAI / thriller.misc.OpenAI.

Task: record this request verbatim in docs/plans/sdk-lookalikes-and-patch-where-used.md, then build it.

Unrelated edits: names shared with other SDKs (chat, completions, create, choices, message, content, ...) must not make a hunk related on their own. A hunk counts as related only through a finding nearby, an openai-bound name (imported from openai, an openai. attribute, or a variable assigned from an openai call or client), or the existing rules. Replay suspense-gptoss-2’s fix.diff: the mock_together hunk must be rejected, and the mock_openai hunk and all app-code hunks kept. Re-run the real-diff check on the earlier runs (djgpt, resume) and report any change.
Patch where it’s used: in the mock-target check, when a test patches "openai.X" (e.g. openai.OpenAI) and a non-test module in the repo binds X with from openai import X, in the candidate or the current working copy, reject with: patch("openai.OpenAI") does not affect src/thriller/api.py, which imports OpenAI with "from openai import OpenAI"; patch "thriller.api.OpenAI" instead (and likewise for: thriller.misc), using the module path the tests import it by. Don’t flag modules that use openai.OpenAI(...) through import openai.

Synthetic fixtures only. Run the fast suite, Docker if the network allows, and ruff, then commit.

## Findings

- **Why mock_together counted as related.** conftest.py has two findings (the
  `patch("openai.ChatCompletion.create")` line and the dict response on the next line). The
  mock_together edits are 5+ lines below them, so they are not "near"; they were kept by the
  name rules, through three leaks:
  1. *attributes*: the near mock_openai hunk accesses `.chat`, `.completions`, `.create`,
     `.return_value`, and mock_together's `mock_client.chat.completions.create.return_value`
     line uses them too;
  2. *names bound by a near hunk, without scope*: mock_openai's new `mock_client = MagicMock()`
     made every `mock_client` in the file related, including mock_together's, a different local
     variable;
  3. *names from import hunks / introduced names*: `from types import SimpleNamespace` (an
     import hunk) and its first use in mock_openai made every `SimpleNamespace(...)` related.
- **The behaviour change.** Inside mock_together the original rebinds `mock_response =
  MagicMock()` and sets `content=mock_response` (the mock itself). The rewrite renames the local
  to `mock_response_obj`, so `content=mock_response` now refers to the module-level string.
- **The patch target.** `src/thriller/api.py` and `misc.py` bind `OpenAI` at import time with
  `from openai import OpenAI`; `patch("openai.OpenAI")` replaces the attribute on the `openai`
  module, which those modules no longer read. The tests import the modules as
  `src.thriller.api` / `src.thriller.misc` (`from src.thriller.api import generate_response`;
  misc.py itself imports `src.thriller.api`), so the target that works in this repo is
  `src.thriller.api.OpenAI`, not `thriller.api.OpenAI`: the request's rule ("the module path
  the tests import it by") gives the former, and that is what is built.

## Plan

### 1. Unrelated edits: openai-bound names, scoped names, no attribute matching

A code hunk that is not near a finding (and not an import hunk) is related only when one of
its nodes (either side, each side read in its own version) is:

- **openai-bound** (new). Per version, a scope-aware, flow-insensitive pass to a fixed point:
  - seeds: the library's module names and every name imported from it;
  - a name (or `self.x` / `cls.x`) assigned from an expression that uses an openai-bound name:
    `=`, annotated and augmented assignment, `:=`, `with ... as`, `for` targets and
    comprehension targets (`client = OpenAI()`, `response = client.chat.completions.create(...)`,
    `self.client = openai.OpenAI()`, `for choice in response.choices`);
  - patch calls whose string target starts with the library (`with patch("openai.OpenAI") as m`,
    `m = mocker.patch("openai...")`), the names they get as `new` / `return_value` /
    `side_effect` / `wraps`, and the test parameters `@patch("openai...")` decorators inject;
  - functions and methods that `return` / `yield` an openai-bound value (`get_client()`,
    `self._client()`), and the parameters of a function in the file that some call passes an
    openai-bound argument to (`first_choice(response)` makes `data` in `def
    first_choice(data)` bound);
  - a library patch call in the hunk itself.
  Scopes are qualified names (`""`, `f`, `C`, `C.m`); a name resolves in its own scope or an
  enclosing one, so `response` in one function does not make `response` in another bound.
- **a name a near code hunk binds or introduces, in the same scope** (the existing rule, now
  scoped): a near hunk in `C.reply` binding `client` relates other hunks in `C.reply` that use
  `client`, not hunks in another function. Module-level bindings count everywhere; `self.x`
  introduced by a near hunk counts in every method of the same class (the threaded `client`
  parameter case). Import aliases no longer count here (library imports are openai-bound).

Dropped: attribute names accessed by near hunks (`.chat`, `.choices`, `.content`, ...), and
names bound by non-library import hunks (`SimpleNamespace`). Kept unchanged: proximity,
import hunks, text-only hunks (revert / keep), the string rule, messages.

Effect on the existing fixture tests: `data["choices"][0]` -> `data.choices[0]` in a function
nothing calls with a response is now rejected (it was kept through the `.choices` attribute);
the same edit is kept when the migrated code calls `first_choice(response)`.

### 2. Patch where it's used (`code_checks.py`, part of the `mock_targets` check)

For each patch target new in a test file's candidate, `<lib>.<path>.<X>` with `<lib>` a library
module and no repo module of that name: every non-test Python module of the repo (the job's
list, judged on the original as everywhere else), read from the working copy (accepted files,
the candidate standing in for its own path), that binds `X` at module level with
`from <lib>.<path> import X [as Y]` and uses that name is not affected by the patch. Not
flagged: modules that only use `<lib>.X` through `import <lib>`, imports inside functions (they
run at call time, under the patch), `if TYPE_CHECKING:` imports, and modules whose own target
the candidate already patches (`patch("src.thriller.api.OpenAI")` anywhere in the file). One
error per target:

```
patch("openai.OpenAI") does not affect src/thriller/api.py, which imports OpenAI with "from openai import OpenAI"; patch "src.thriller.api.OpenAI" instead (and likewise for: src.thriller.misc)
```

Module path: each dotted name the module has under the test's module roots (its own dir, the
test plan's import paths, the root, `src/`), choosing the one the repo's test files import
(`import a.b`, `from a.b import x`, `from a import b`; the candidate included), else the one
the repo's non-test modules import, else the shortest. An alias is shown in the list as
`mod (as Y)`.

The agent lists the repo's Python files once per job (walker, original repo) and splits them
with `is_test_file`; `check_candidate` passes both lists to `mock_target_problems`.

### Tests (synthetic)

- New fixture `tests/fixtures/sdk_lookalike/`, modelled on the pattern (no copied code): a
  package under `src/` with a module calling `openai.ChatCompletion.create` beside a second,
  made-up SDK (`acmechat`, whose client has `chat.completions.create` too), a conftest.py with
  an openai mock fixture and an acmechat mock fixture, and tests that import the package as
  `src.<pkg>.<module>`.
- `tests/test_unrelated.py`: the lookalike replay (the acmechat fixture's rewrite rejected, the
  openai fixture and app code kept), scope (same local name in another function), taint
  through assignment, `with patch(...) as`, decorators, returns, parameter passing, `self.x`;
  the `first_choice` change above.
- `tests/test_code_checks.py`: the message (exact), several modules, aliases, module path
  from test imports / module imports / shortest, imports in functions and under
  TYPE_CHECKING, `import openai` users, unused imports, targets already patched, targets
  present in the original.
- `tests/test_whole_file.py` / `tests/test_agent.py`: wiring (the lists reach the check).

After building: replay suspense-gptoss-2 (and the other suspense runs) and re-run the real-diff
check on djgpt and resume; record the results under "Built".

## Built

Items 1 and 2 as planned. Changes from the plan as written:

- **Data access is not an object.** For return values and arguments (functions returning a
  bound value, parameters of functions called with one), only a bound name, `self.x` or a call
  rooted at one counts, not attribute or subscript access that does not end in a call. Without
  this, `_clean(response.choices[0].message.content)` made `_clean`'s `text` bound, and an
  unrelated refactor of `_clean` was kept (an existing test caught it). Assignments still take
  the whole chain (`text = r.choices[0].message.content` relates later uses of `text` in that
  function).
- **Introduced `self.x` and threaded parameters.** A near hunk's new `self.client` relates
  `self.client` and the name `client` in methods of the same class (the old rule, file-wide by
  name, now limited to the class), so a `client` parameter added to `__init__` is still kept.
- **A test that uses the library name itself is not flagged.** djgpt-gptoss-1's
  tests/test_ci.py patches `openai.OpenAI` and then calls `openai.OpenAI()` itself (a test of
  the mock); the first version flagged it because prompt.py's shim imports `OpenAI as
  _OpenAI`. A test file that uses `<library>.X` through `import <library>` now passes.
- **Patched by the wrong path.** A module counts as patched only by the path the tests import
  it by, when that is known (a test file or another module imports it); otherwise any of its
  paths counts. The message names a patch by another path: `("thriller.api.OpenAI" names it by
  a path the tests do not import it by)`. In SuspensePerception, hatch packages `src` and
  pytest adds no `src/` to `sys.path`, so `thriller.api` is not the module the tests load.
- **Message**: as requested, with the module path the tests use (`src.thriller.api.OpenAI` in
  this repo, not `thriller.api.OpenAI`; see Findings) and "(and likewise for: ...)" listing
  module paths (`mod (as Y)` when a module binds another name).
- The fixture is `tests/fixtures/sdk_lookalike/` (made-up second SDK `acmechat`).

### Real-diff check (2026-10-23)

Each run's `fix.diff` applied to a copy of the original repo (`~/apifix-targets/`), the
original scanned, and each changed Python file run through `revert_text_edits`,
`unrelated_edits` and the openai-v1 `rewrite_checker`, plus (test files) `mock_target_problems`
with the repo's module and test-file lists, as `check_candidate` does (scratch script, not
committed). Before/after is this commit against its parent.

| Run | Unrelated edits / config | Mock targets (new check) |
|---|---|---|
| djgpt-gptoss-1, -3, -4 | unchanged (hosted-1/3: prompt.py:190 reverted; cli.py:69 check A; hosted-3 prompt.py:132 check B) | none |
| resume-2, resume-gptoss-1, -1-verify, -2 | unchanged (resume-2: `__main__` block; hosted-1: `_create_dummy_pdfs`, the PDF refactor, testapp.py:109 string; -1-verify: the same string) | none |
| suspense-gptoss-2 | **new:** conftest.py original lines 22 (`mock_response = MagicMock()`) and 26 (`mock_client.chat.completions.create.return_value = mock_response`) rejected: the Together mock. mock_openai, the import and every app-code hunk kept | **new:** `patch("openai.OpenAI") does not affect src/thriller/api.py, which imports OpenAI with "from openai import OpenAI"; patch "src.thriller.api.OpenAI" instead (and likewise for: src.thriller.misc)` |
| suspense-gptoss-1, suspense-verify-1, -2, -3 | unchanged (nothing) | **new:** the same for api.py, plus `("thriller.api.OpenAI" names it by a path the tests do not import it by)`; misc.py calls `openai.OpenAI()` through `import openai` there, so it is not listed |

### Tests

- `tests/fixtures/sdk_lookalike/` (synthetic): `src/storyteller/llm.py` calls openai and a
  made-up `acmechat` SDK with the same client shape; `scoring.py` calls openai; conftest.py
  mocks both (the acmechat mock rebinds a local `canned_reply`, like the real pattern); tests
  import `src.storyteller.*`.
- `tests/test_unrelated.py`: the lookalike conftest (other SDK's mock rejected at lines 22 and
  26, openai mock kept), the app code next to the other SDK (kept; the other SDK's call edited
  with the same names rejected), openai-bound vs lookalike pairs (same function vs another
  function's `response`, the other SDK's `chat.completions.create`, a client from `get_client()`
  vs `get_acme()`, `self.client` vs `self.acme`), patch-decorator parameters, and
  `first_choice`: rejected alone, kept when called with the response.
- `tests/test_code_checks.py`: the exact message on the fixture, `import openai` modules not
  listed, modules not migrated yet, both modules patched by the tests' path, patched by
  another path (note), targets already in the original, module path choice (tests' import,
  modules' import, shortest, package `__init__`), imports inside functions / under
  TYPE_CHECKING / unused, a test using `oa.OpenAI()` itself, aliases and the "likewise" list,
  a submodule target, a file with no dotted name, a repo module named `openai`.
- `tests/test_agent.py`: a whole-file replay on the fixture: conftest.py patching
  `openai.OpenAI` goes back through check retry with the exact message, and the reply patching
  `src.storyteller.llm.OpenAI` is accepted.
