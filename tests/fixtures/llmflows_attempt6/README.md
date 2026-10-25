`llmflows/llms/openai_embeddings.py` from llmflows (https://github.com/stoyan-stoyanov/llmflows,
MIT License, commit 3c9e59f) and two whole-file rewrites of it from the llmflows-gptoss-3 run.

- openai_embeddings_original.txt: the file as in the repo (0.x API).
- openai_embeddings_attempt6_reply.txt: attempt 6's reply, verbatim, which put
  `openai.Embedding.create(engine=...)` and `.acreate` back; every check passed it and the
  verifier rejected it (incomplete_migration at lines 77, 85, 118, 126).
- openai_embeddings_attempt5.txt: the version in attempt 5's tree (accepted in attempt 1 and
  kept through attempt 5; only likely findings remain in it).

Stored as .txt: they are data for the completeness check, not code to lint or collect.
