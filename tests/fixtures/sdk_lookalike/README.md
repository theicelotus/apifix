# sdk_lookalike

Synthetic fixture for apifix (openai 0.x): a package under `src/` that calls openai beside a
second chat SDK, `acmechat` (made up; never installed), whose client has the same shape as
openai 1.x (`client.chat.completions.create`, `.choices[0].message.content`). conftest.py
mocks both. The tests import the package as `src.storyteller`.
