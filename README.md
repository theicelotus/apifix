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
apifix verify path/to/repo --migration openai-v1 --diff apifix-out/<run>/fix.diff  # no LLM
apifix cleanup [--dry-run]                              # recover after a crash
```

`apifix fix` works on a copy of the repo (yours is never modified). It writes `fix.diff`,
`report.md`, `verification.json` and `trajectory.json` to `./apifix-out/<timestamp>/` (or
`--out`). Exit code 0 means verified (possibly with weak evidence), 1 not verified, 2 aborted
or a usage error. Model prices used for `--max-cost` can be overridden with `--prices FILE`
(TOML); check them against Anthropic's pricing page.

`apifix verify` re-checks a saved diff (e.g. a previous run's `fix.diff`, or one written by
hand) without any LLM call: it applies the diff to a copy of the repo with `git apply` and runs
the same baseline and verification as `fix`, writing the same four files. Use it when a run
could not be judged, for example because the original code's dependency install failed on a
slow network. Diffs that do not apply, reach outside the repo or create symlinks are refused.

If the original code's tests cannot run (its install failed or timed out), tests that fail on
the fixed code are not counted as regressions: they may have failed before too. The verdict is
then "unverified" (or "verified, with weak evidence" when every test passes), and the report
lists the failing tests and suggests `apifix verify` for a later re-check.

Tests run with pytest in the sandbox, which also runs unittest tests. Test files are found by
name (`test*.py`, `*_test.py`, anything under `tests/`) or by content (imports unittest or
pytest and defines `Test*` classes or `test_*` functions) and passed to pytest explicitly. When
a test imports a neighbouring module by bare name (`backend/testapp.py`: `from app import ...`),
that directory goes on `PYTHONPATH`. After the fixed code's dependencies are installed, a
smoke check makes sure the new library actually works with them (openai: create a client). If
it fails, the fix is rejected with the installed versions and the pins that can hold the
library back (e.g. `typing_extensions==4.5.0` forcing an old openai). If the library is not
installed at all because no dependency file apifix reads declares it, the report says that
instead (verdict "unverified"); if the fix removed its declaration, the fix is rejected.

### Dependencies in the sandbox

apifix reads `pyproject.toml` (`[project]`, dependency groups, Poetry, and pixi's
`pypi-dependencies`, including features and Linux targets), `pixi.toml`, `setup.py`/`setup.cfg`,
`Pipfile`, requirements files and the `pip:` list of a conda `environment.yml`. Some declared
packages are not installed, and the report lists them under "Declared dependencies not
installed":

- conda-only packages (pixi `dependencies`, an environment's conda list): not supported yet;
- pixi targets for other platforms (`osx-*`, `win-*`), and pip packages that only install on
  macOS or Windows (`pyobjc*`, `appnope`, `pywin32`, ...);
- `environment.yml` next to a pixi manifest (pixi never reads it), and the conda list of an
  exported environment (`conda env export`: build strings, `prefix:`).

When the original code's tests import modules that the sandbox does not have and no test
passes, the verdict is "unverified" with `environment_incomplete`. The report names each missing
module, where it is declared, and why it was not installed. No edit to the code can change
that, so the fix is not retried, and the model is never told to change pins because of it.

`--sandbox-requirements FILE` (for `fix` and `verify`) replaces apifix's dependency detection
with your own requirements file for both the original code and the fix. The fix's own
dependency edits still apply: for every package whose declaration the fix changed, the fix's
version replaces your line (e.g. your `openai==0.28` becomes the fix's `openai>=1.0`), and the
report lists each replacement. The file may hold requirements and `--index-url` /
`--extra-index-url` lines (e.g. a CPU-only torch index), but no includes, editables, local paths
or URLs with credentials, since it is passed to pip in the networked install container.

```
apifix verify path/to/repo --migration openai-v1 --diff fix.diff \
    --sandbox-requirements sandbox-requirements.txt
```

Dependency installs share a pip cache (`~/.cache/apifix/pip`, or `$APIFIX_PIP_CACHE`), so a
package downloaded once is reused by later runs, and the baseline and each attempt no longer
download everything again. Only the install phase sees it; the test phase still has no
network. The report says how much each run downloaded and took from the cache.
`--no-pip-cache` turns it off. The cache is filled by install containers, which run
third-party packages' build scripts, so a malicious package could leave files there that a
later run installs; the repo's own code never runs with the cache or the network.

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
server's message. Some servers also count the reply tokens a request allows for against the
per-minute token limit. In whole-file mode apifix sizes that allowance from the file being
rewritten (about twice its size / 3 tokens, plus 2,048 for models that reason first; at most
`--max-output-tokens`). When a request is still refused as larger than the per-minute limit
("Limit 8000, Requested 9265"), it is sent once more with the allowance lowered to fit, as long
as that leaves room for the file; otherwise the job stops and says why. Lower
`--max-file-bytes` or use a model with a higher limit then.

`apifix cleanup` removes sandbox containers and temp dirs that an interrupted run (crash,
kill, power loss) left behind. Runs that are still in progress are not touched: each one holds
a lock that the operating system releases when the process dies. Use `--dry-run` to see what
would be removed.

## Live results

The results now include real repos as well as runs of the live test
`test_local_whole_file_fix_completes` (a small openai 0.x fixture repo). Real repos are linked,
not copied: without a license that allows it, their code, the diff and the trajectory stay out
of this repo.

| Date | Repo | Model | Mode | Hardware | Attempts | Time | Tokens | Verdict | Trajectory |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15 | fixture (live test) | `qwen2.5-coder:7b` (Ollama) | whole-file | CPU only | 2 | 48 min | 12,825 | verified | [JSON](benchmark/live/2026-10-15-qwen2.5-coder-7b-whole-file-verified.json) |
| 2026-10-22 | [brianAbell/AI-powered-Resume-Analyzer-and-Feedback-Generator](https://github.com/brianAbell/AI-powered-Resume-Analyzer-and-Feedback-Generator) | `openai/gpt-oss-120b` (hosted free tier, $0) | whole-file | hosted | 1 | 140 s | 12,675 | verified | not published (repo has no license) |

2026-10-22: a Flask app on openai 0.28.1, CRLF line endings, tests in `backend/testapp.py`.
Time: LLM 57 s, sandbox 82 s with the pip cache warm. All 8 findings resolved, 100%
changed-line coverage; 4 tests that need PDF files missing from the repo fail before and after
the fix, so they count as no signal, not as passes.

See `CLAUDE.md` for scope, architecture and rules.
