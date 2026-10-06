# apifix

Automatically fixes Python code broken by library breaking changes, verifies each fix in a
network-less sandbox, and reports the evidence.

Current status: the scanner for the OpenAI 0.x -> 1.x migration, the Docker test sandbox
(`apifix.engine.sandbox`), the verifier (`apifix.engine.verifier.verify`), and the fix agent
(`apifix fix`, Claude via the `anthropic` SDK).

```
pip install -e '.[dev]'                                   # or '.[anthropic]' to just run fixes
apifix scan path/to/repo --migration openai-v1          # human-readable
apifix scan path/to/repo --migration openai-v1 --json   # machine-readable
ANTHROPIC_API_KEY=... apifix fix path/to/repo --migration openai-v1 --max-cost 5
apifix cleanup [--dry-run]                              # recover after a crash
```

`apifix fix` works on a copy of the repo (yours is never modified). It writes `fix.diff`,
`report.md`, `verification.json` and `trajectory.json` to `./apifix-out/<timestamp>/` (or
`--out`). Exit code 0 means verified (possibly with weak evidence), 1 not verified, 2 aborted
or a usage error. Model prices used for `--max-cost` can be overridden with `--prices FILE`
(TOML); check them against Anthropic's pricing page.

`apifix cleanup` removes sandbox containers and temp dirs that an interrupted run (crash,
kill, power loss) left behind. Runs that are still in progress are not touched: each one holds
a lock that the operating system releases when the process dies. Use `--dry-run` to see what
would be removed.

See `CLAUDE.md` for scope, architecture and rules.
