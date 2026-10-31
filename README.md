# apifix

Apifix migrates Python code across breaking library API changes. An LLM
rewrites the affected code; apifix then runs the project's own tests before and
after the change in an offline Docker sandbox, rejects removed or weakened
tests, and measures how much of the changed code the tests actually ran.

## What it does

When a library ships a breaking change (for example, the OpenAI Python SDK 1.0
removed `openai.ChatCompletion.create`), apifix:

1. **finds** the affected code (Python `ast`, no LLM);
2. **rewrites** the affected files with an LLM;
3. **verifies** the result: the project's tests run in a Docker sandbox with no
   network, before and after the change; tests are compared one by one, and
   removed or weakened tests are rejected;
4. **reports** a verdict, and can turn a verified fix into a pull request that
   states it was written by an AI model.


## Supported migrations

- `openai-v1`: OpenAI Python library 0.x -> 1.x
- `google-genai`: google-generativeai (support ended 2025-11-30) -> the Google Gen AI SDK
  (`google-genai`)

## Installation

Requirements: Python 3.11+, Docker, git. The GitHub CLI (`gh`) is needed only
to open pull requests.

```
pip install apifix
```

For development:

```
git clone https://github.com/theicelotus/apifix.git
cd apifix
pip install -e '.[dev]'
```

## Usage

```
apifix scan path/to/repo --migration openai-v1            # what would change

export APIFIX_LLM_API_KEY=...                             # key for your endpoint
apifix fix path/to/repo --migration openai-v1 \
    --provider openai-compat --base-url <endpoint> --model <model> \
    --edit-mode whole-file --no-cost-limit                # fix and verify

apifix pr apifix-out/<run> --repo path/to/repo            # branch + PR text
```

`--base-url` takes any OpenAI-compatible endpoint. With a local model through
Ollama (`--base-url http://localhost:11434/v1 --model qwen2.5-coder:14b`), your
code never leaves your machine. Claude is the default provider
(`ANTHROPIC_API_KEY`). Free hosted tiers may keep what you send, so use them
only for public code.

Your repository is never modified: apifix works on a copy and writes
`fix.diff`, `report.md`, `verification.json` and `trajectory.json` to
`apifix-out/<timestamp>/`.

## Tested on

8 public open-source repos, with the open-weight model `gpt-oss-120b`:

| Repo | Result |
|---|---|
| [brianAbell/AI-powered-Resume-Analyzer-and-Feedback-Generator](https://github.com/brianAbell/AI-powered-Resume-Analyzer-and-Feedback-Generator) | verified |
| [biobootloader/wolverine](https://github.com/biobootloader/wolverine) | verified |
| [openai/bugbounty-gpt](https://github.com/openai/bugbounty-gpt) | verified |
| [rgbkrk/genai](https://github.com/rgbkrk/genai) | verified |
| [MattOates/djgpt](https://github.com/MattOates/djgpt) | verified, weak evidence |
| [HCAI-Lab-GT/SuspensePerception](https://github.com/HCAI-Lab-GT/SuspensePerception) | verified, weak evidence |
| [stoyan-stoyanov/llmflows](https://github.com/stoyan-stoyanov/llmflows) | verified, weak evidence |
| [saulpw/aipl](https://github.com/saulpw/aipl) | verified, weak evidence |

*Verified*: the tests pass before and after, and they run at least half of the
changed lines. *Weak evidence*: the tests pass but run less than half of the
changed lines, so those lines are listed for review by hand. Diffs, reports and
how each run was made: [docs/guide.md](docs/guide.md#live-results).

## Documentation

[docs/guide.md](docs/guide.md): all commands and options, the sandbox, model
choices (local and hosted), pull requests, and detailed results.

## License

MIT
