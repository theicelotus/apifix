# apifix-pr-sandbox

A tiny support-bot library written against the openai 0.28 API, with tests. It exists so
`apifix pr --push` can be tried end to end on a repository you own (see docs/pr-sandbox.md in
apifix). It is synthetic: modelled on apifix's `openai_v0` fixtures, nothing here is real code.

- `supportbot/chat.py`: `ChatCompletion.create`, dict-style responses, `openai.error`
- `supportbot/search.py`: `Embedding.create`
- `tests/`: unittest-style pytest tests that mock the 0.x API; they pass on openai 0.28.1

Run the tests with `pip install -r requirements.txt && pytest`.
