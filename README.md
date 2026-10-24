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
`--out`); the last three write your home directory as `~`, so they can be published as they
are. Exit code 0 means verified (possibly with weak evidence), 1 not verified, 2 aborted
or a usage error, 3 already migrated: the code is already on the new API (every declared
`openai` requirement needs 1.x, non-test code uses 1.x names) and every finding is in a test
file, so the tests are out of date rather than the code. `fix` then stops before any LLM call
or sandbox run and lists those findings; `scan` reports the same case. Model prices used for `--max-cost` can be overridden with `--prices FILE`
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

When the verdict is "verified, with weak evidence" or "unverified", the report also lists the
changes no test executed under "Review by hand", one entry per changed region: removed lines,
code turned into a comment and edits inside strings are always listed, since coverage cannot
vouch for them.

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

### Dependency pins

apifix sets the migrated library's pin itself, with no LLM call (`openai>=1.0,<2` for
openai-v1), in every format it reads: requirements files, `pyproject.toml` (`[project]`,
dependency groups, Poetry, pixi), `pixi.toml`, `Pipfile`, `setup.py`/`setup.cfg` string
literals and an `environment.yml` `pip:` list. Only the version is rewritten; names, extras,
markers, comments, quoting and line endings stay as they were, and every edit is checked by
parsing the file again. In the same files, pins of openai's own requirements that would hold it
back are raised to what openai needs (e.g. `typing_extensions==4.5.0` ->
`typing_extensions>=4.11,<5`; pydantic stays on its major version). The report lists each pin
apifix set ("apifix set: requirements.txt:3 openai==0.28.1 -> openai>=1.0,<2"). Entries it
cannot rewrite safely (continuation lines, escaped TOML strings, non-literal `setup.py` values)
go to the model as before, and the report says why; hash-pinned requirements files are left
for a manual update. Conda entries and lockfiles are never edited: the report names conda
entries that would conflict and lockfiles to regenerate.

A declaration that still allows 0.x gets the pin too: `openai` with no version, `*`, or a
range such as `openai>=0.27` or `openai<2` (a [likely] finding, since pip alone would pick a
new release, but a lockfile or an existing environment can keep 0.x). Direct references
(`openai @ https://...`, git or path entries) are left alone.

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

pytest keeps going past test files that fail to import (`--continue-on-collection-errors`,
unless the repo's own pytest options stop at the first failure), so one test file that needs a
package the sandbox lacks no longer stops the others. A file that fails to import both before
and after the fix counts as already failing, not as a regression.

When the original code's tests import modules that the sandbox does not have and nothing else
usable runs (no test passes, and none fails for another reason), the verdict is "unverified"
with `environment_incomplete`. The report names each missing module, where it is declared,
and why it was not installed. No edit to the code can change that, so the fix is not retried,
and the model is never told to change pins because of it. When other tests do run, the missing
modules are only listed, with the test files they block.

Before the first sandbox run, apifix also reads the code for third-party imports that no
dependency file declares (and a `--sandbox-requirements` file does not list), and the report
lists them under "Imported but not declared", with the likely pip name (`yaml` -> `pyyaml`,
`sklearn` -> `scikit-learn`, `cv2` -> `opencv-python`) and the files that import them. This
is information only: nothing is installed because of it.

`--sandbox-requirements FILE` (for `fix` and `verify`) replaces apifix's dependency detection
with your own requirements file for both the original code and the fix. The fix's own
dependency edits still apply: for every package whose declaration the fix changed, the fix's
version replaces your line (e.g. your `openai==0.28` becomes the fix's `openai>=1.0`), and the
report lists each replacement. When the fix leaves the library's declaration unchanged, your
line for it still gets the migration's version in the fixed run if it allows the old one
(`openai==0.28.1` becomes `openai>=1.0,<2`; the original code keeps your line), and the report
says so. If the fixed code's smoke check then fails and the only pins that can hold the library
back are in your file (e.g. `typing_extensions==4.5.0`), the verdict is "unverified" with
`sandbox_requirements_pin`: a problem with the file, not the fix. The model is not told about
it and the fix is not retried. The file may hold requirements and `--index-url` /
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
function the test calls, nor a library name such as `openai.OpenAI` that a module imports with
`from openai import OpenAI` (the patch never reaches it; the error names the module's own
target, by the path the tests import it by); the error names the target to patch instead
where it can (when the patched module never touches the library, the modules the test reaches
that do, each with its own target), and a failing file gets it back once (a fresh request with the rejected version as a diff, not the
whole rejected file). A file that fails again is set aside for that attempt while the
others are still sent (tests importing a module that failed wait for the next attempt), and
the next attempt sends only what failed, so one stubborn file does not use up the attempts
before anything is verified. A file that fails a check with the same error on two attempts in a row
is told so; on the third, the job stops instead of spending the remaining attempts. Edits away from the migration are caught as well: a changed string,
docstring or comment far from any finding (a word slipped into a prompt) is put back by apifix
and listed in the report ("apifix reverted: ..."); unrelated code changes, or string literals
that went missing, send the file back. Code counts as related through a finding nearby or a name
tied to the library (imported from it, or assigned from its calls and clients), never through
names other SDKs share, so a second chat SDK's `client.chat.completions.create` mock is left
alone. For openai-v1, so does dropping a configured
`openai.api_key` / `organization` / `api_base` value (a CLI option, say) or adding an
`OpenAI()` that would ignore it: 1.x's `OpenAI()` never reads `openai.api_key`, so the guide
says to keep the assignment and use the module-level client (`openai.chat.completions.create`).
These checks are heuristics (see CLAUDE.md for what they miss) and run in whole-file mode only
for now. Dependency files whose only findings are pins apifix set are not sent.
All files are then verified together exactly as in agent mode,
and a failed verification starts the next attempt from the original files, with the failing
test output and each file's previous version as a diff.

**Large files: region edits.** Sending a whole file and getting it back costs its size twice,
which a free tier's per-minute token limit cannot always take. In whole-file mode a Python file
over 8 KB (`--region-threshold`) is sent as regions instead: only the top-level functions,
methods and statements that hold a finding, plus the import block, each in a labelled code
block, with the names they use and an outline of the rest as read-only context. The model
returns each region in its own labelled block, apifix splices them back by position, and the
complete file goes through all the checks above. A failing check resends only the regions it
concerns; regions too large for one request go in several, and once a per-minute limit is
known every request, retries and later attempts included, is split into groups that fit it
(a refused one too); a later attempt also sends the
regions of tests that failed. `--file-edits whole|regions` forces either form. The model can
only change those regions (and add imports, assignments and helper functions at the top), so
a fix that needs other code outside them is out of reach until a test failure points there.

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
as that leaves room for the file; otherwise the file is sent again as regions. From then on
apifix keeps the limit and trims requests to fit it: each file gets only its own guide
sections, less failing-test output, and test files see the other changed files as their changed
statements or just the lines that use the library; a file that would not fit whole goes as
regions, and regions that would not fit one request (a retry, or a later attempt with each
region's previous version) go in groups, with the reply allowance lowered in advance instead of
waiting for a refusal. If a single region is still refused, the job stops and says why; use a
model with a higher limit then.

Network blips: once the endpoint has answered, a connection that fails mid-job (DNS failure,
refused, reset) is retried after 30, 60 and 120 s before the job stops; the report notes the
waits. A server that is not reachable at the start (not running, wrong URL) still fails at once.

`apifix cleanup` removes sandbox containers and temp dirs that an interrupted run (crash,
kill, power loss) left behind. Runs that are still in progress are not touched: each one holds
a lock that the operating system releases when the process dies. Use `--dry-run` to see what
would be removed.

## Live results

The results now include real repos as well as runs of the live test
`test_local_whole_file_fix_completes` (a small openai 0.x fixture repo). Real repos are linked;
their code, the diff and the trajectory are copied here only when the repo's license allows it.

| Date | Repo | Model | Mode | Hardware | Attempts | Time | Tokens | Verdict | Trajectory |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-15 | fixture (live test) | `qwen2.5-coder:7b` (Ollama) | whole-file | CPU only | 2 | 48 min | 12,825 | verified | [JSON](benchmark/live/2026-10-15-qwen2.5-coder-7b-whole-file-verified.json) |
| 2026-10-22 | [brianAbell/AI-powered-Resume-Analyzer-and-Feedback-Generator](https://github.com/brianAbell/AI-powered-Resume-Analyzer-and-Feedback-Generator) | `openai/gpt-oss-120b` (hosted free tier, $0) | whole-file | hosted | 1 | 140 s | 12,675 | verified | not published (repo has no license) |
| 2026-10-23 | [MattOates/djgpt](https://github.com/MattOates/djgpt) | `openai/gpt-oss-120b` (hosted free tier, $0) | whole-file | hosted | 1 | 156 s | 20,750 | verified_weak | not published |
| 2026-10-23 | [HCAI-Lab-GT/SuspensePerception](https://github.com/HCAI-Lab-GT/SuspensePerception) | `openai/gpt-oss-120b` (hosted free tier, $0) | whole-file | hosted | 1 | 271 s | 16,168 | verified_weak | not published (repo has no license) |
| 2026-10-23 | [biobootloader/wolverine](https://github.com/biobootloader/wolverine) | `openai/gpt-oss-120b` (hosted free tier, $0) | whole-file | hosted | 1 | 153 s | 17,571 | verified | [folder](benchmark/live/2026-10-23-wolverine-gptoss/) |
| 2026-10-23 | [openai/bugbounty-gpt](https://github.com/openai/bugbounty-gpt) | `openai/gpt-oss-120b` (hosted free tier, $0) | whole-file | hosted | 1 | 317 s | 13,972 | verified | [folder](benchmark/live/2026-10-23-bugbounty-gpt-gptoss/) |

2026-10-22: a Flask app on openai 0.28.1, CRLF line endings, tests in `backend/testapp.py`.
Time: LLM 57 s, sandbox 82 s with the pip cache warm. All 8 findings resolved, 100%
changed-line coverage; 4 tests that need PDF files missing from the repo fail before and after
the fix, so they count as no signal, not as passes.

2026-10-23 (djgpt): an MIT-licensed CLI with its dependencies in pixi and conda files, including
speech libraries that cannot install in the sandbox, so it ran with `--sandbox-requirements`.
Time: LLM 105 s (83 s of it rate-limit waits), sandbox 43 s. 23 tests passed and 1 skipped,
before and after the fix. apifix set both openai pins itself, without the model, and reverted
one unrelated text edit the model made. The CLI's API key assignment was kept and the call moved
to the module-level client. No test runs the 2 changed lines in `src/djgpt/prompt.py`, so the
report lists them for review by hand.

2026-10-23 (SuspensePerception): a research project that declared openai without a version
while `uv.lock` had 1.99.2 and the code used the 0.x API, so it was broken for anyone
installing it. apifix set `openai>=1.0,<2` itself and noted `uv.lock` for regenerating. Heavy
NLP packages the repo imports but does not declare made it run with `--sandbox-requirements`.
Time: LLM 70 s, sandbox 193 s. The repo's own suite fails 11 of its 13 tests before and after
the fix (a fixture called directly, an outdated function signature), so apifix reports them as
no signal, each with its reason. The fixed test mock patches `src.thriller.api.OpenAI` and
`src.thriller.misc.OpenAI`, where the code uses the name.

2026-10-23 (wolverine): an MIT-licensed tool (~5k stars) that reruns a crashing script and asks
GPT-4 to fix it, pinned to openai 0.27.4. Time: LLM 86 s (66 s of it rate-limit waits), sandbox
66 s. 6 of 6 tests pass before and after the fix and run 67% of the changed lines. apifix set
`openai==0.27.4` -> `openai>=1.0,<2` itself; the fix also moves `openai.Model.list()` to
`client.models.list()`.

2026-10-23 (bugbounty-gpt): OpenAI's MIT-licensed classifier for Bugcrowd submissions, with
Poetry dependencies and `poetry.lock`. Time: LLM 54 s (40 s of it rate-limit waits), sandbox
255 s, mostly first-time downloads. 25 of 25 tests pass before and after the fix and run every
changed line. apifix set the Poetry pin `openai = "0.28.1"` -> `">=1.0,<2"` itself and noted
`poetry.lock` for regenerating.

`verified_weak` means the tests pass but do not exercise the migrated code, so the fix still
needs a human review.

See `CLAUDE.md` for scope, architecture and rules.
