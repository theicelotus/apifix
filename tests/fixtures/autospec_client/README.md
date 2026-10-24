# autospec_client

Synthetic fixture for apifix (openai 0.x), modelled on genai: the tests patch
`openai.ChatCompletion.create` with `autospec=True` (a function, so autospec checks its
signature). A migration that moves the patch to the client class and keeps `autospec=True`
(`patch("assist.generate.OpenAI", autospec=True)` + `OpenAI.return_value.chat.completions`)
breaks: an autospecced 1.x client has no `chat.completions`, since the client's resources are
cached properties. `tests/test_generate.py` is the original; the candidates are in
apifix's tests (tests/test_code_checks.py).
