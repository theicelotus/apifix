# stale_tests

Synthetic fixture for apifix: the code is already on openai 1.x (pinned in requirements.txt
and Pipfile, `openai.OpenAI` in `src/topics/gen.py`), but the tests still mock the 0.x API
(`openai.ChatCompletion.create` patch targets, a dict response). `src/outline /` has a trailing
space in its name on purpose.
