# keyless_client

Synthetic fixture for apifix (openai 0.x), modelled on genai: `helper/handler.py` catches
every error from the suggestion code, and the test patches `openai.ChatCompletion.create`. A
migration that builds `openai.OpenAI()` inside `suggest()` and moves the patch to the resource
method (`openai.resources.chat.completions.Completions.create`) breaks without
`OPENAI_API_KEY`: the real constructor still runs and raises, the handler swallows the error,
and the test sees only "Called 0 times". `tests/test_handler.py` is the original; the
candidates are in apifix's tests (tests/test_unkeyed_patches.py).
