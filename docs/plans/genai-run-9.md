# genai run 9: placeholder module names, added call assertions, resend only what failed

## Request

Context: Read CLAUDE.md and docs/plans/genai-run-8.md. Run ~/apifix-runs/genai-gptoss20b-3/ (gpt-oss-20b, default reasoning): attempt 2 reached verification; apifix verify on its diff (~/apifix-runs/genai-gptoss20b-3/attempt2.diff, output in ~/apifix-runs/genai-gptoss20b-3-verify2/) is rejected for 2 regressions. Attempt 3 then resent genai/generate.py, tests/test_context.py and tests/test_generate.py, which the failures didn’t involve, and the replies were cut off until the daily quota ran out (211k tokens in total). Record this request verbatim in docs/plans/genai-run-9.md, then build it.

ModuleNotFoundError: No module named 'app' in tests/test_magics.py::test_assist_magic_with_fresh_arg: the model patched an app.… target, probably copied from the guide’s examples, which use app as the sample module. (a) The mock-target check should reject a target whose top-level module is neither a repo module nor an importable or declared package. (b) In the guide and mock examples, use a clearly placeholder module name (e.g. yourpkg.module), or substitute the repo’s actual module that uses the library when building the prompt.
Expected 'create' to have been called once. Called 0 times in tests/test_suggestions.py::test_custom_exc_error_inside: it passed the new key check, so find the actual cause from the diff (patch target vs how generate.py looks up the client in this version, or something else) and fix whatever check or guidance should have caught it.
Resend only what the failures implicate: after a verification rejection, resend only the files that failing tests live in, the modules their tracebacks or the verifier’s reasons point to, and files with remaining findings, not every Python file. Files not implicated keep their accepted versions. Explain the rule in the plan and replay this run’s attempt 3 to show which files it would send.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

`~/apifix-runs/genai-gptoss20b-3-verify2/` (`apifix verify` of attempt 2's diff): 36 passed / 8
failed against the baseline's 38 / 6, two regressions.

- **`tests.test_magics::test_assist_magic_with_fresh_arg`: `No module named 'app'`.** All three
  tests in test_magics.py patch `app.OpenAI`; the other two fail on the network either way (no
  signal), so only this one shows. `app` is the guide's sample module (`app.openai.OpenAI`,
  `app.client...`, `app.get_client`, `from app import ask`) and the autospec hint's
  (`patch("app.OpenAI")`). Nothing checked the target: `_missing_name_problem` only judges
  targets whose prefix is a repo module, and treats anything else as "outside the repo".
- **`tests.test_suggestions::test_custom_exc_error_inside`: `Called 0 times`. Not the key, not
  the target.** The test patches `genai.generate.OpenAI`, which is right: attempt 2's
  generate.py does `from openai import OpenAI` and builds `OpenAI()` in both functions. So no
  real client is built and no key is needed (run 8's key check rightly passed it). The cause is
  an assertion the model *added*: the original test never asserts `create` (its `display` mock
  raises `TypeError` inside `custom_exc` before `generate_exception_suggestion`), and attempt 2
  appended `OpenAI.return_value.chat.completions.create.assert_called_once()` (plus an unused
  `args, kwargs = ...call_args`). The code under test never reaches `create` in that test.
- **The feedback pointed the wrong way.** Attempt 3's requests carried run 8's uncalled-mock
  hint ("genai/generate.py:56 builds OpenAI() without api_key= ... patch the client class
  (`patch("genai.generate.OpenAI")`)"): the test already patched exactly that.
- **Confirmed in the sandbox**: attempt 2's diff with only those two changes (the two added
  lines removed from test_custom_exc_error_inside; `app.OpenAI` -> `genai.generate.OpenAI` in
  test_magics.py) is **verified**, 38 / 6 like the baseline.
- **Attempt 3 resent three files the failures did not involve.** `_after_rejection` resent
  every Python file unless every failure was a mock failure (run 6's exception); neither
  failure here is one, so genai/generate.py, tests/test_context.py and tests/test_generate.py
  went back before test_magics.py (test_suggestions.py was next), their replies were cut off
  (reasoning tokens), and the daily quota ran out during test_magics.py's request.

## Plan

1. **(a) Unknown modules in mock targets** (`code_checks._unknown_module_problem`, check
   `mock_targets`): a new target whose first name is no repo module and nothing the test run
   can import is rejected: "patch() fails with ModuleNotFoundError", plus the modules that use
   the library with their targets and an example (the existing "patch where a module the test
   reaches uses it" list, now also without a patched module). Importable
   (`imports.ImportableModules`, once per job from the original): stdlib and builtin modules,
   `ALWAYS_INSTALLED`, the migration's library and companion modules, every name the repo's
   Python files import, and declared distributions (the "imported but not declared" mapping,
   --sandbox-requirements included).
2. **(b) A placeholder sample module**: the guide and the hints use `yourpkg.module`
   (`yourpkg/module.py`), and the Tests part says it is a placeholder for the dotted path the
   tests import the code under test by. Substituting the repo's real module when building the
   prompt was the alternative; not done, because the right target depends on how each module
   binds the library (`<module>.OpenAI` for `from openai import OpenAI`, `openai.OpenAI` for
   `import openai`, `<module>.client...`, `<module>.get_client`), the examples show all of
   these forms, and the check messages from (a) and the earlier mock-target checks already name
   the real module and the form that fits it. A model that copies `yourpkg.module` gets (a)'s
   message.
3. **The added assertion** (`code_checks.added_call_assertions`, new check `call_assertions`,
   test files, whole-file mode): per test the original has, call assertions (`assert_called*`,
   `assert_any_call`, `assert_has_calls`, `assert_awaited*`, ...; `assert m.called` /
   `m.call_count`) aligned with the original's by kind; a test with more than before is
   rejected, naming the added one and what the original asserts on. A migrated assertion (same
   kind, new mock path) is not counted. The Tests part says to migrate each test's assertions
   and add none.
4. **The hint**: for uncalled-mock failures in tests that patch a client class
   (`code_checks.patches_client_class`), `Migration.uncalled_patched_hint` names the tests: no
   real client is built, so the code did not reach the call; compare with the original (an
   added call assertion), else check the target. Run 8's key hint only when some uncalled test
   does not patch a client class. Agent mode gets the same (it has no whole-file checks).
5. **Resend only what the failures implicate** (`agent.implicated_files`, replacing the
   all-Python-files rule and run 6's mock-only exception, which it subsumes). The rule: after a
   rejection, the next attempt sends, among the files whole-file mode can send,
   - the files the failing tests live in (newly failing / erroring tests, and tests that
     reject / unverified reasons name: collection errors as their module, failures without a
     baseline);
   - files the failing tests' output points at (`path:N`, `"path", line N`), except non-test
     files in the output of a test that failed on its own mock (only where the code called the
     mock: run 6), and files in the fixed run's error;
   - files that reject / unverified reasons name (tampering, incomplete migration, ...;
     warnings such as scope or suspicious additions and info reasons such as `setup_changed`
     do not), and files with remaining findings;
   - dependency files whose install or smoke check failed and that the install read (as
     before);
   - and, because an assertion failing in the test never names the code under test: for a
     failing test whose own file cannot be sent (no findings), and for one that already failed
     the previous verification, the non-test files its file reaches through imports.

   Everything else keeps its accepted version. When nothing is implicated (a run that failed
   without naming any file), every Python file goes, as before. Why the last bullet: with the
   rule alone, a wrong value returned by the code under test (`assert ask("q") == "x"` failing
   in the test) would resend only the test, and the code could never be fixed. Widening only
   after a narrowed attempt failed keeps the first resend minimal (genai: no generate.py) at the
   cost of one more attempt in that case. Recorded on the rejected attempt as `resend` (path ->
   why).
6. Synthetic fixture `early_error` (genai's shape: a handler that shows a placeholder, then the
   suggestion, and catches everything; a test whose display raises first). Tests: a pytest run
   against the stub client (run 8's) shows both failures as the run had them and both fixes
   passing; the checks; the hint; `implicated_files` cases; agent-level tests for the
   escalation and the hint.

## Built

As planned. Notes:

- The unknown-module check has a known false positive: a package installed only as another's
  dependency that the repo never imports (genai: `requests`, via tiktoken) reads as unknown.
  Reading the installed distributions from the baseline's environment would fix it; not done.
- `added_call_assertions` counts only call assertions, not `call_args` reads, so the run's
  dangling `args, kwargs = ...call_args` (which would raise on `None` too) is not reported on
  its own; it sits in the same hunk as the rejected assertion.
- When a narrowed resend comes back unchanged (the test was right, the code was wrong), that
  tree is verified again before the code under test is widened in: one more sandbox run in
  that case. Skipping the run for a tree already judged was left out to keep attempts simple.
- Existing tests whose failures stood for "app.py is wrong" with a bare `AssertionError` now
  carry an app.py frame in their output (the shared judge's `# BREAK` is an assert inside
  app.py), as a real traceback would; tests for the dependency-only and remaining-finding
  resends now expect only that file back.

Replay (`replay_run9.py`, offline: attempt 2's verification from the run's trajectory into
`implicated_files`, with reachability over attempt 2's tree; then `check_candidate` with the
run's settings on attempt 2's test files):

| | Files |
|---|---|
| attempt 3 in the run | genai/generate.py, tests/test_context.py, tests/test_generate.py, tests/test_magics.py (quota ran out; test_suggestions.py next) |
| attempt 3 now | tests/test_magics.py (failing test `test_assist_magic_with_fresh_arg`), tests/test_suggestions.py (failing test `test_custom_exc_error_inside`) |
| attempt 4, if both still failed | + genai/generate.py (imported by tests/test_magics.py, its test still failing) |

pyproject.toml (set by apifix) and the other files keep their attempt 1 / 2 versions.

Attempt 2's test files under the new checks: test_context.py and test_generate.py accepted;
the other two rejected:

> [mock_targets] mock target 'app.OpenAI': there is no module 'app' (not a repo module, the
> standard library, a declared dependency or a module the repo imports), so patch() fails with
> ModuleNotFoundError: No module named 'app'. A module name in an example is a placeholder: use
> the dotted path the tests import the code under test by. Patch openai where a module the test
> reaches uses it:
> genai/generate.py: generate_next_from_history() creates client = OpenAI():
> patch("genai.generate.OpenAI") and set OpenAI.return_value.chat.completions.create.return_value.
> (then the objects note and an example)

> [call_assertions] line 249: test_custom_exc_error_inside adds a call assertion the original
> test does not make: `OpenAI.return_value.chat.completions.create.assert_called_once()` (the
> original makes 1, on ip.showtraceback). Migrate the test's existing assertions and add no new
> ones: the original test may not reach that call (an error raised earlier, a branch not
> taken), so the new assertion fails

The hint attempt 3's feedback would now carry instead of the missing-key one
(test_custom_exc_error_inside patches `genai.generate.OpenAI`):

> a mock was never called in tests.test_suggestions::test_custom_exc_error_inside, which patch
> the client class, so no real client is built and no API key is needed: the code under test
> did not reach the call in that test. Compare the test with the original: a call assertion the
> original does not make (create.assert_called_once() in a test whose code raises or returns
> before the call) expects a call the code never makes there; keep the test's assertions as
> they were. Otherwise check that the patch target is where the code under test looks the
> client up
