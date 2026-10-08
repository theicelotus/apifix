# apifix

Automatically fixes Python code broken by library breaking changes, verifies each fix in a
network-less sandbox, and reports the evidence.

Current status: the scanner for the OpenAI 0.x -> 1.x migration, the Docker test sandbox
(`apifix.engine.sandbox`), the verifier (`apifix.engine.verifier.verify`), and the fix agent
(`apifix fix`, with Claude via the `anthropic` SDK or any OpenAI-compatible endpoint, including
free local models).

```
pip install -e '.[dev]'                                   # or '.[anthropic]' to just run fixes
apifix scan path/to/repo --migration openai-v1          # human-readable
apifix scan path/to/repo --migration openai-v1 --json   # machine-readable
ANTHROPIC_API_KEY=... apifix fix path/to/repo --migration openai-v1 --max-cost 5
apifix fix path/to/repo --migration openai-v1 --provider openai-compat --model qwen2.5-coder:14b
apifix cleanup [--dry-run]                              # recover after a crash
```

`apifix fix` works on a copy of the repo (yours is never modified). It writes `fix.diff`,
`report.md`, `verification.json` and `trajectory.json` to `./apifix-out/<timestamp>/` (or
`--out`). Exit code 0 means verified (possibly with weak evidence), 1 not verified, 2 aborted
or a usage error. Model prices used for `--max-cost` can be overridden with `--prices FILE`
(TOML); check them against Anthropic's pricing page.

## Free local models (Ollama, LM Studio, vLLM)

`--provider openai-compat` talks to any OpenAI-compatible chat-completions endpoint that
supports tool calling. Local endpoints (localhost or a private-network address) cost $0;
apifix's token and tool-call limits still apply.

```
# Ollama (default endpoint: http://localhost:11434/v1)
ollama pull qwen2.5-coder:14b
OLLAMA_CONTEXT_LENGTH=32768 ollama serve      # the default context is far too small
apifix fix path/to/repo --migration openai-v1 --provider openai-compat --model qwen2.5-coder:14b

# LM Studio / vLLM: point --base-url at the server
apifix fix ... --provider openai-compat --base-url http://localhost:1234/v1 --model <name>
apifix fix ... --provider openai-compat --base-url http://gpu-box.local:8000/v1 --model <name>
```

- `--model` is required; without it apifix lists the server's models and suggests a starting
  model for your hardware (table below).
- Before any sandbox work, apifix checks that the model really makes tool calls and fails with
  a clear error if it does not (many small models and some chat templates cannot).
- Some models write a tool call as their reply text instead of in the tool-call field (e.g.
  `qwen2.5-coder:7b` leaves out the `<tool_call>` tags Ollama looks for). apifix uses such a
  reply as a call only if the whole reply is exactly one `{"name": ..., "arguments": {...}}`
  object (optionally in one ```` ```json ```` fence or `<tool_call>` block) naming one of its
  tools, with valid arguments. A reply with prose around the JSON is just text. A reply holding
  several such calls runs none of them; the model is asked for one call per reply (at most 3
  times per attempt). The report counts these calls, the trajectory marks each one
  (`"source": "parsed_from_text"`), and `--strict-tool-calls` turns this off. For such models
  the system prompt also shows one example call, and a reply that asks for files instead of
  calling a tool (before any edit) is answered with a reminder to call one (at most 3 times
  per attempt).
- Raise the context window. Agent conversations reach 30-100k tokens; a server that silently
  truncates prompts would drop apifix's rules. apifix reads Ollama's context window and
  stops with instructions if the conversation would not fit, and detects truncation on other
  servers where it can.
- Local models are slower and less reliable than Claude at multi-step edits: expect more
  retries and `rejected` results. The verifier is the same, so a wrong fix is reported as such,
  never as verified.

**Hardware** (rough figures for 4-bit quantised models, Ollama's default):

| Hardware | Starting model | Weights | Notes |
|---|---|---|---|
| 8 GB GPU, or 16 GB RAM CPU-only | `qwen2.5-coder:7b` | ~5 GB | Fast on a GPU; on CPU a job can take an hour. Tiny repos only |
| 12-16 GB GPU, or 32 GB RAM / Apple Silicon | `qwen2.5-coder:14b` | ~9 GB | Noticeably better tool use |
| 24 GB GPU, or 32-48 GB unified-memory Mac | `qwen2.5-coder:32b` | ~20 GB | Smallest size recommended for real repos |
| 48 GB+ GPU, or 64 GB+ Mac | `llama3.3:70b` | ~40 GB | Best local quality, slow |

Add a few GB on top of the weights for a 32k-token context (KV cache).

**Hosted endpoints** (e.g. free tiers of hosted inference services) work the same way with
`--base-url https://...` and an API key in `APIFIX_LLM_API_KEY` (or the variable named by
`--api-key-env`; `OPENAI_API_KEY` is never read). Keys are only sent over https. A hosted
endpoint needs a price in the `--prices` TOML file (use 0 for a free tier) or
`--no-cost-limit`, because free tiers can turn into paid ones. apifix prints a notice that
repository code will be sent to that third-party server. **Some hosted free tiers may use
submitted data for training**: check the provider's terms before sending private code.

`apifix cleanup` removes sandbox containers and temp dirs that an interrupted run (crash,
kill, power loss) left behind. Runs that are still in progress are not touched: each one holds
a lock that the operating system releases when the process dies. Use `--dry-run` to see what
would be removed.

See `CLAUDE.md` for scope, architecture and rules.
