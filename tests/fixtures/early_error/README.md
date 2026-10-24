# early_error

Synthetic fixture for apifix (openai 0.x), modelled on genai-gptoss20b-3. `cellhelp/handler.py`
shows a placeholder, then a suggestion, and catches every error. `test_on_error_inside` makes the
display raise first, so the suggestion call is never reached and the test never asserts it. A
migration that adds `create.assert_called_once()` there fails with "Called 0 times", and one that
patches `app.OpenAI` (the guide's old sample module) fails with "No module named 'app'". The
files here are the originals; the candidates are in apifix's tests (tests/test_early_error.py).
