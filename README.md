# apifix

Automatically fixes Python code broken by library breaking changes, verifies each fix in a
network-less sandbox, and reports the evidence.

Current status: the scanner for the OpenAI 0.x -> 1.x migration, the Docker test sandbox
(`apifix.engine.sandbox`), and the verifier (`apifix.engine.verifier.verify`).

```
pip install -e '.[dev]'
apifix scan path/to/repo --migration openai-v1          # human-readable
apifix scan path/to/repo --migration openai-v1 --json   # machine-readable
apifix cleanup [--dry-run]                              # recover after a crash
```

`apifix cleanup` removes sandbox containers and temp dirs that an interrupted run (crash,
kill, power loss) left behind. Runs that are still in progress are not touched: each one holds
a lock that the operating system releases when the process dies. Use `--dry-run` to see what
would be removed.

See `CLAUDE.md` for scope, architecture and rules.
