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

Tests run with pytest in the sandbox, which also runs unittest tests. Test files are found by
name (`test*.py`, `*_test.py`, anything under `tests/`) or by content (imports unittest or
pytest and defines `Test*` classes or `test_*` functions) and passed to pytest explicitly. When
a test imports a neighbouring module by bare name (`backend/testapp.py`: `from app import ...`),
that directory goes on `PYTHONPATH`. After the fixed code's dependencies are installed, a
smoke check makes sure the new library actually works with them (openai: create a client). If
it fails, the fix is rejected with the installed versions and the pins that can hold the
library back (e.g. `typing_extensions==4.5.0` forcing an old openai).

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
- Each request may take up to an hour on a local endpoint (10 minutes on a hosted one) before
  apifix gives up with a timeout error. On a CPU-only machine a large file can need longer:
  raise it with `--request-timeout SECONDS`.
- Local models are slower and less reliable than Claude at multi-step edits: expect more
  retries and `rejected` results. The verifier is the same, so a wrong fix is reported as such,
  never as verified.

**Whole-file mode (recommended for small local models).** Models around 7B often cannot drive
the tool loop, but can rewrite a file. `--edit-mode whole-file` uses no tools: each file with
findings is sent in its own request (test files also see the updated code they test, read-only),
and the model returns the complete updated file in one code block. Prompts carry only the guide
sections the repo's findings need, and are laid out so a server's prompt cache can reuse them. Before a file is written it must
parse as Python (or as a dependency file), differ from the original when it has certain
findings, contain no placeholders such as `# ... rest unchanged`, use no name it never imports
or defines, and (test files) patch neither a repo-module name that does not exist nor the
function the test calls; the error names the target to patch instead where it can, and a
failing file gets it back once. All files are then verified together exactly as in agent mode,
and a failed verification starts the next attempt from the original files, with the failing
test output and each file's previous version as a diff.

```
apifix fix path/to/repo --migration openai-v1 --provider openai-compat \
    --model qwen2.5-coder:7b --edit-mode whole-file [--max-file-bytes 24000]
```

Files over the size budget (24,000 bytes by default, file plus context) are not sent and are
listed in the report; their findings stay, so the fix is reported as incomplete. Works with any
provider.

**Hardware** (rough figures for 4-bit quantised models, Ollama's default):

| Hardware | Starting model | Weights | Notes |
|---|---|---|---|
| 8 GB GPU, or 16 GB RAM CPU-only | `qwen2.5-coder:7b` | ~5 GB | Fast on a GPU; on CPU a job can take an hour. Tiny repos only; use `--edit-mode whole-file` |
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

## Fast testing with a hosted free tier

Hosted free tiers answer in seconds where a 7B model on a CPU takes most of an hour. the hosted provider's
endpoint is OpenAI-compatible. **Free tiers may use submitted requests for training: only send
public code.**

1. Create an API key at https://console.example.com/keys and put it in `APIFIX_LLM_API_KEY` (or
   any variable, named with `--api-key-env NAME`). The key is only sent over https, and never
   appears in the trajectory, report or error messages.
2. Give the model a price of 0, so the cost limit stays on for every other model. Save as
   e.g. `~/.config/apifix/prices.toml`, one table per model you use:

   ```toml
   [models."llama-3.3-70b-versatile"]
   input = 0
   output = 0
   cache_read = 0
   cache_write = 0
   ```

3. List the current models (they change often) and run:

```
export APIFIX_LLM_API_KEY=...
export APIFIX_PRICES=~/.config/apifix/prices.toml       # or --prices FILE
curl -s https://api.example.com/openai/v1/models -H "Authorization: Bearer $APIFIX_LLM_API_KEY" \
    | python3 -c 'import json,sys; print("\n".join(sorted(m["id"] for m in json.load(sys.stdin)["data"])))'
apifix fix path/to/repo --migration openai-v1 --provider openai-compat \
    --base-url https://api.example.com/openai/v1 --model llama-3.3-70b-versatile \
    --edit-mode whole-file --max-output-tokens 6000
```

`--no-cost-limit` works too, but turns the limit off for that run whatever the model costs.

Rate limits: free tiers allow a few dozen requests and a few thousand tokens per minute per
model (see https://console.example.com/settings/limits). On HTTP 429 apifix waits as long as
the server asks (`Retry-After`, or "try again in 7.5s"), or backs off 2, 4, 8 ... 60 s, and
retries: up to 6 times per request, each wait at most 2 minutes. The waits are not failures;
the report notes how many there were. A daily or quota limit stops the job at once with the
server's message. Each request also counts the reply tokens it allows for (16,000 by default)
against the per-minute token limit on some servers. If requests are refused as too large
(HTTP 413), lower `--max-output-tokens` (a whole file needs roughly its size / 3 tokens) or
`--max-file-bytes`.

`apifix cleanup` removes sandbox containers and temp dirs that an interrupted run (crash,
kill, power loss) left behind. Runs that are still in progress are not touched: each one holds
a lock that the operating system releases when the process dies. Use `--dry-run` to see what
would be removed.

## Live results

Runs of the live test `test_local_whole_file_fix_completes` (a small openai 0.x fixture repo):

| Date | Model | Mode | Hardware | Attempts | Time | Tokens | Verdict | Trajectory |
|---|---|---|---|---|---|---|---|---|
| 2026-10-15 | `qwen2.5-coder:7b` (Ollama) | whole-file | CPU only | 2 | 48 min | 12,825 | verified | [JSON](benchmark/live/2026-10-15-qwen2.5-coder-7b-whole-file-verified.json) |

See `CLAUDE.md` for scope, architecture and rules.
