# apifix

Automatically fixes Python code broken by library breaking changes, verifies each fix in a
network-less sandbox, and reports the evidence.

Current status: the scanner for the OpenAI 0.x -> 1.x migration, and the Docker test sandbox
(`apifix.engine.sandbox.run_sandbox`).

```
pip install -e '.[dev]'
apifix scan path/to/repo --migration openai-v1          # human-readable
apifix scan path/to/repo --migration openai-v1 --json   # machine-readable
```

See `CLAUDE.md` for scope, architecture and rules.
