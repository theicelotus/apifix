# genai run 8: patched resource methods with a real client and no API key

## Request

Context: Read CLAUDE.md and docs/plans/genai-run-7.md. apifix verify on genai-gptoss-8 attempt 2 (~/apifix-runs/genai-gptoss-8-verify2/) is rejected for 3 regressions in tests/test_suggestions.py: Expected 'create' to have been called once. Called 0 times. The tests now patch openai.resources.chat.completions.Completions.create (autospec, following our “autospec the method instead” advice), while genai/generate.py builds a real openai.OpenAI() first. I believe that raises without OPENAI_API_KEY (the sandbox has none; genai’s CI sets none), and genai’s suggestion code swallows the error, so create is never reached. Record this request verbatim in docs/plans/genai-run-8.md, confirm the cause with a sandbox run, then build:

Guidance: in the Tests guide part, the autospec advice and the mock examples, say that patching the method (...Completions.create) leaves the real client constructor running, which needs an API key, so the test must also set one (monkeypatch.setenv("OPENAI_API_KEY", "test") / mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"})) unless the code passes api_key=. Otherwise patch the client class without autospec. Prefer the client-class form in the examples.
Check: reject a new or changed patch of a resource method (openai.resources....create, any path) in a test that sets no API key (env patch, fixture, conftest, or a key passed in), when the code under test constructs a client without api_key=. Message with both fixes. Replay this diff: tests/test_suggestions.py must be rejected with it.
Failure hint: for Expected 'create' to have been called once. Called 0 times (or called 0 times on a library method) when the code constructs a client: mention the missing-key cause and that errors may be swallowed by the code’s own try/except.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

`~/apifix-runs/genai-gptoss-8-verify2/` (`apifix verify` of `~/apifix-runs/genai-gptoss-8/attempt2.diff`):
genai/generate.py now builds `client = openai.OpenAI()` inside `generate_next_from_history` and
`generate_exception_suggestion` (no `api_key=`), and tests/test_suggestions.py patches
`openai.resources.chat.completions.Completions.create` with `autospec=True` in four tests. The
other test files patch `genai.generate.openai.OpenAI` (the client class, no autospec) and pass.

Confirmed with sandbox runs (`apifix verify` on the run's diff with small changes to
tests/test_suggestions.py only; same image, same baseline 38 passed / 6 failed):

| Variant of test_suggestions.py | Fixed run | Result for the three tests |
|---|---|---|
| the run's diff, test_custom_exc records what custom_exc prints | 35 / 9 | `call('Error while trying to provide a suggestion: ', OpenAIError('The api_key client option must be set either by passing api_key to the client or by setting the OPENAI_API_KEY environment variable'))` |
| + `@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"})` on the three tests, autospec kept | 35 / 9 | `create` is called now, but `assert args == ()` fails: `Left contains one more item: <openai.resources.chat.completions.completions.Completions object>` |
| + the key, autospec dropped | 38 / 6 | verified |
| client class instead (`@mock.patch("genai.generate.openai.OpenAI")`, no autospec, no key) | 38 / 6 | verified |

- **The cause is the one the request names.** `openai.OpenAI()` raises `OpenAIError` without
  `OPENAI_API_KEY` (the sandbox sets none: genai's CI sets none), genai/suggestions.py's
  `except Exception` prints it, and `create` is never reached.
- **A second trap behind it.** The method patched with `autospec=True` is a function on the
  class, so it binds: the mock records the `Completions` instance as its first argument, and
  genai's `assert args == ()` fails even with a key. Our advice ("autospec the method instead")
  leads into both traps; the client class without autospec avoids both.
- test_custom_exc_error_inside patches the method too but passes either way (its display mock
  raises before the client is built).

## Plan

1. Guidance (Tests guide part): patch the client class without autospec (the examples lead
   with it); patching the method leaves the real constructor running, so unless the code
   passes `api_key=` the test must also set a key (`@mock.patch.dict(os.environ,
   {"OPENAI_API_KEY": "test"})` / `monkeypatch.setenv("OPENAI_API_KEY", "test")`), shown as a
   last example; the autospec bullet drops "autospec the method instead" and says an
   autospecced method gets the resource object as `args[0]`. The autospec check's message and
   the `Mock object has no attribute 'completions'` hint say the same.
2. Check (`code_checks.unkeyed_method_patch_problems`, check `mock_targets`, whole-file mode):
   a new or changed string patch of `openai.resources.<path>.<Class>.<method>` (any module
   prefix) applying to a test that gets no key (environment patch or a key passed in the test,
   its class or setup, a fixture it uses, module-level code or setup hooks of the test file or
   a conftest above it, pytest-env settings), while a non-test module the test reaches through
   imports builds `OpenAI()` / `AsyncOpenAI()` in a function without `api_key=`. Message with
   both fixes and the module's client class target. Off when the repo's CI sets the key (the
   sandbox then has a placeholder). Migration data: `Migration.client_key` (`ClientKey`).
3. The unrelated-edit check must accept the fix it suggests: code hunks whose candidate side
   names `OPENAI_API_KEY` are related (otherwise `@mock.patch.dict(os.environ, ...)` above a
   test's decorators, far from the finding's line, was rejected: seen in the first replay).
4. Failure hint (`Migration.uncalled_mock_hint`, markers `verifier.UNCALLED_MOCK_MARKERS`:
   `Called 0 times`, `Actual: not called`): when the working copy builds a client without a key
   and CI sets none, the missing key, errors swallowed by the code's own try/except, both fixes.
   Whole-file and agent mode.
5. Synthetic fixture `keyless_client` (a handler that catches every error, as genai's), tests
   including a pytest run against a stub client that raises without a key: the rejected shape
   fails with "Called 0 times", both suggested fixes pass, the key with autospec kept fails on
   `args[0]`.

## Built

As planned. Deviation: the check follows the request's rule, not the code path, so
test_custom_exc_error_inside (passes either way) is named too; adding the key there is
harmless. Clients built at module level are not checked (they fail at import, before any test
can set a key).

Replay (`replay_run8.py`, offline: `check_candidate` with the run's settings on genai's
original and the run's tree as the working copy):

| tests/test_suggestions.py | Result |
|---|---|
| the run's (method, autospec, no key) | **rejected** (mock_targets), message below |
| the key on the three failing tests only | rejected: test_custom_exc_error_inside sets no key |
| client class, no autospec | accepted |

The run's test_generate.py, test_magics.py and test_context.py stay accepted.

> mock target 'openai.resources.chat.completions.Completions.create' patches a method, so the
> real client is still built: genai/generate.py:66 calls openai.OpenAI() without api_key=, which
> raises when OPENAI_API_KEY is not set (tests run without it, and the code's own try/except may
> hide the error), and test_custom_exc sets no key (and likewise in:
> test_custom_exc_fallback_on_In, test_custom_exc_long_traceback, test_custom_exc_error_inside).
> Set a placeholder key in the test (@mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}) or
> monkeypatch.setenv("OPENAI_API_KEY", "test")), or patch the client class instead, without
> autospec: patch("genai.generate.openai.OpenAI") and set
> OpenAI.return_value.chat.completions.create.return_value

The hint the run's feedback would now carry (attempt 2's tree):

> a mock was never called, and genai/generate.py:66 builds openai.OpenAI() without api_key=: with
> no OPENAI_API_KEY set (the tests run without one) the constructor raises OpenAIError before the
> call, and the code's own try/except may swallow the error. Set a placeholder key in the test
> (mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test"}) or monkeypatch.setenv("OPENAI_API_KEY",
> "test")), or patch the client class where the code looks it up
> (patch("genai.generate.openai.OpenAI"), without autospec)
