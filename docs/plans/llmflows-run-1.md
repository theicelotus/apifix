# llmflows run 1: mixed response access, attribute mock data, conflicting dependency sources

## Request

Context: Read CLAUDE.md. llmflows run ~/apifix-runs/llmflows-gptoss-1/ (~/apifix-targets/llmflows) stopped with repeated_check_error after 7 attempts that never reached verification. Record this request verbatim in docs/plans/llmflows-run-1.md, then build it.

Mixed response access: llmflows reads responses as model_outputs.choices[0]["text"] and model_outputs.choices[0]["message"]["content"] (attribute, then subscript), which 0.x objects allowed. The scanner doesn’t flag these, so the model’s correct rewrites (.choices[0].text, .choices[0].message.content) were rejected as unrelated edits (attempts 1–2 in llmflows/llms/azure_openai.py, azure_openai_chat.py, openai.py). Flag any subscript with a response key ("text", "message", "content", "delta", "embedding", "data", "usage", …) applied after .choices[...] / .data[...] / .usage, and the reverse mix, as response_access. Check the embeddings shapes (["data"][0]["embedding"], .data[0]["embedding"]) too.
Mock data assigned through attributes: tests/llms/test_openai.py has model_outputs = MagicMock(); model_outputs.choices = [{"text": "test_text"}] (original lines 68–69), and converting it was rejected 5 times. Treat dict-shaped mock data assigned to .choices / .data (and .return_value chains) as a mock response finding, like the existing dict-literal rule.
Conflicting dependency sources: the baseline install failed with ResolutionImpossible: tiktoken==0.5.1 and tiktoken==0.3.3, because apifix installed requirements.txt and pyproject.toml together and they pin different versions. A user installs from one source, not both. When dependency sources conflict on exact pins, install from one source: prefer the one the repo’s CI or README install command uses, else pyproject.toml (the package’s own metadata), and report which source was used and which conflicting pins were ignored. Also, when the install fails with ResolutionImpossible, put pip’s conflict lines in the report, and don’t retry it as a network failure.
Replay the run’s replies against the changed scanner and checks, and report which files would now be accepted. Confirm the baseline install now succeeds on llmflows.

Synthetic fixtures. Fast suite, Docker if the network allows, and ruff, then commit.

## What the run shows

- **Response access through a parameter.** llmflows' `_format_results(self, model_outputs,
  ...)` reads `model_outputs.choices[0]["text"]` (openai.py:70, azure_openai.py:88) and
  `model_outputs.choices[0]["message"]["content"]` (openai_chat.py:79, azure_openai_chat.py:93).
  The scanner only flags string-keyed access on a name it saw assigned from an openai call in
  the same scope; a parameter is never one. With no finding on those lines, the unrelated-edit
  check rejected the model's correct `.choices[0].text` rewrites (attempt 1: three files;
  attempt 2: openai.py again). `openai_embeddings.py` was accepted: `result =
  openai.Embedding.create(...)` is in the same function, so `result["data"][i]["embedding"]`
  was already a finding.
- **Mock data through an attribute.** tests/llms/test_openai.py:68-69, `model_outputs =
  MagicMock(); model_outputs.choices = [{"text": "test_text"}]`. The dict-literal rule needs a
  `{"choices": [...]}` literal, so nothing was flagged; attempts 3-7 rejected every conversion
  (line 68 when the model replaced the MagicMock, line 69 when it changed the list).
- **The baseline never installed.** apifix installs every declared source together:
  pyproject.toml (`tiktoken==0.5.1`, `pinecone-client==2.2.4`) and requirements.txt
  (`tiktoken==0.3.3`, `pinecone-client==2.2.1`). requirements.txt is fully pinned, so its pins
  were also tried as lockfile constraints (dropped as conflicting), then the install was retried
  without them, which could not help: pip's conflict lines say "The user requested" for both
  pins, neither "(constraint)". The report said nothing about the failure: the job aborted
  before any verification, and the report shows install problems only inside one. The repo's
  CI (`.github/workflows/test.yml`, `pylint.yml`) runs `pip install -r requirements.txt`; the
  README's `pip install llmflows` is the package's metadata (pyproject.toml).

## Plan

1. **Mixed response access** (scanner, `python_checks.py`, files with an openai import, any
   root, [likely] `response_access`): an attribute/subscript chain is flagged when
   - *forward*: an attribute `.choices` / `.data` followed by an index subscript, then
     (anywhere later in the chain) a string-keyed subscript whose key is a response key, or
     `.usage` followed by a string-keyed subscript with a token key
     (`x.choices[0]["text"]`, `x.choices[0]["message"]["content"]`, `x.choices[0].message
     ["content"]`, `x.data[0]["embedding"]`, `x.usage["total_tokens"]`);
   - *reverse*: a string-keyed subscript `["choices"]` / `["data"]` followed by an index, then
     an attribute that is a response key, or `["usage"]` followed by a token-key attribute
     (`x["choices"][0].text`, `x["data"][0].embedding`, `x["usage"].total_tokens`).

   Response keys: text, message, content, delta, role, finish_reason, index, logprobs,
   function_call, tool_calls, name, arguments (choices); embedding, index, object, url,
   b64_json, revised_prompt (data); prompt_tokens, completion_tokens, total_tokens (usage).
   1.x objects are not subscriptable and its lists hold no dicts, so the shape is 0.x code
   whatever the root. The suggestion gives the rewritten expression (string keys that are
   identifiers become attributes): "1.x returns typed objects ...: use
   model_outputs.choices[0].text". The finding is the chain up to its last response part (not
   a trailing `.strip()`); tracked responses keep their existing finding (no duplicate). The
   openai-import condition keeps out other SDKs whose responses allow both (litellm); a helper
   in a module without an openai import is missed (documented). Pure-subscript chains
   (`x["data"][0]["embedding"]`) stay flagged only on tracked responses, as before (a plain dict
   is not a 0.x object); tests confirm the embeddings shapes.
2. **Mock data through attributes** (test files, no openai import needed, like the dict-literal
   rule): an assignment whose target is an attribute `.choices` / `.data` / `.usage` (any
   chain: `m.return_value.choices`, `create.return_value.data`), and a call keyword
   `choices=` / `data=` / `usage=` (`MagicMock(choices=...)`, `SimpleNamespace(data=...)`,
   `dict(choices=...)`), whose value is dict-shaped mock data: a list/tuple holding a dict with
   a choice key (message, text; delta: the chunk suggestion as for literals) for `choices`,
   with `embedding` for `data`, a dict with a token key for `usage`. Finding on the statement
   (assignment) or the keyword. Suggestion: build them as objects, e.g.
   `model_outputs.choices = [SimpleNamespace(text="test_text")]` (the literal's own first key
   and value when it is a string constant). Selects the Tests part (test file) and Responses.
3. **One source per conflicting set** (`project.build_install_plan`): two dependency files
   conflict when an exact pin (`name==v`, no marker, not a direct reference) in one is not
   allowed by the other's requirement for that name (also no marker). Conda environment files
   are not compared (their pip list already yields by name). When files conflict, they are
   ranked: used by an install command in the repo's GitHub Actions workflows, then in the
   README (`README*` at the root), then package metadata (pyproject.toml, setup.cfg, setup.py),
   then the existing order; each is kept unless it conflicts with one kept before. Install
   commands: `pip install` (also `pip3`, `python -m pip`, `uv pip`) with `-r` /
   `--requirement FILE` (the file), `.` / `-e .` / `.[extras]` or the project's own name (the
   metadata files); `poetry install` (pyproject.toml); `pipenv install` (Pipfile). A dropped
   file installs nothing; its entries not declared elsewhere are listed under "Declared
   dependencies not installed" ("conflicts with requirements.txt, which was installed").
   `InstallPlan.dropped_sources` (file, the kept file, why that one, the conflicting pins) goes
   into the run summary, the notes, the job report ("Dependency sources conflict: ...") and
   the verification report. Files not in any conflict are installed as before. A fully pinned
   requirements file dropped this way is not used as a lockfile either.
4. **Resolution failures** (`sandbox.py`): a failed install whose output has
   `ResolutionImpossible` / "conflicting dependencies" is never retried as transient, even with
   a network marker in the output (pip's "from versions: none" outages stay retried: they print
   "No matching distribution", not ResolutionImpossible). It is retried without the lockfile
   only when pip's conflict lines involve a constraint ("(constraint)") or name none at all.
   pip's conflict lines ("Cannot install ...", "The conflict is caused by:" and its list; the
   generated manifest's `-r /manifest/requirements.txt (line N)` shown as the requirement) are
   kept on the run (`install_conflict`, run summary), with the repo files that declare each
   package named, and shown in the report: in the verification report when either run failed
   that way, and in the job report for a baseline that failed when no verification follows.
5. Synthetic fixtures: response-access shapes (forward, reverse, embeddings, usage, tracked
   and untracked, no openai import, trailing calls), attribute and keyword mock data, a repo
   with conflicting pyproject.toml / requirements.txt and a CI workflow (and the README and
   metadata fallbacks), pip conflict output for the parser and the retry rules; report lines.
   Replay: the run's rejected replies through `check_candidate` with the new findings. Docker:
   the baseline install of llmflows.

## Built

As planned (items 1-5). Notes:

- Correction to "What the run shows": openai_embeddings.py's `result["data"][i]["embedding"]`
  (lines 91, 132) was *not* a finding. The response comes back through `call_with_retry(func=
  openai.Embedding.create, ...)`, which the scanner does not track; the run accepted the
  rewrite only because the unrelated-edit check binds `result` through that call. The new
  keys-only rule (`rules.KEY_ONLY_KEYS`) flags both lines now.
- Keys only (`x["choices"][i]["message"]`, `x["data"][i]["embedding"]`) is flagged on any root
  in a file with an openai import, unless the root is known to be a plain dict (dict literal,
  `dict()`, `json.load(s)`, `.model_dump()` / `.to_dict()` / `.json()`, a name last assigned
  one in the scope, or a name under a dict guard). Known false positive: a plain dict that comes
  in as a parameter in such a file. `["usage"][...]` and `["data"][i]["url"]` keys only are not
  flagged (a plain dict reads the same).
- Mock data through attributes also covers `.usage = {...}`, `.choices[i].message = {...}` /
  `.delta = {...}` and the call keywords `choices=` / `data=` / `usage=`, so
  `dict(choices=[{"message": {}}])`, listed as missed before, is a finding now.
- Install-command detection is a per-line regex over workflow and README text (no YAML parser,
  no shell `cd`). llmflows has two workflows; `pylint.yml` sorts first, so the report names it.
- A conflict with no "(constraint)" line in pip's list stops the lockfile retry with a note
  ("pip's conflict involves no ... pin; not retried without them").

Replay (`replay.py`, offline: every whole-file reply in the run's trajectory through
`check_candidate` with the run's settings; the working copy at each entry is llmflows' original
plus the files the run had accepted before it, from the run's fix.diff). Run once with HEAD's
code (a git worktree) and once with the new code. HEAD's results match the run's report
reply for reply.

| Attempt | File | Run (HEAD) | Now |
|---|---|---|---|
| 1 | llmflows/llms/azure_openai.py (2 replies) | rejected: unrelated line 88 | **accepted** (both) |
| 1 | llmflows/llms/azure_openai_chat.py (2) | rejected: unrelated line 93 | **accepted** (both) |
| 1 | llmflows/llms/openai.py (2) | rejected: unrelated line 70; config line 136 | rejected: config line 136 only |
| 1 | llmflows/llms/openai_chat.py reply 1 | rejected: unrelated line 79 | **accepted** |
| 1 | llmflows/llms/openai_chat.py reply 2 | accepted | accepted |
| 1 | llmflows/llms/openai_embeddings.py reply 1 / 2 | config line 119 / accepted | same |
| 2 | llmflows/llms/azure_openai.py reply 1 | rejected: unrelated line 88 | **accepted** |
| 2 | llmflows/llms/azure_openai_chat.py | regions, accepted | not replayed (regions) |
| 2 | llmflows/llms/openai.py (2) | rejected: unrelated line 70; config line 136 | rejected: config line 136 only |
| 3 | llmflows/llms/openai.py | accepted | accepted |
| 3 | tests/llms/test_openai.py (2) | rejected: unrelated line 68 | **accepted** (both) |
| 3 | tests/llms/test_openai_chat.py, test_openai_embeddings.py | accepted | accepted |
| 4-7 | tests/llms/test_openai.py (8 replies) | rejected: unrelated line 68 / 69 | **accepted** (all 8) |

Every "unrelated to the migration" rejection in the run is gone. The config error that remains
on openai.py (`openai.AsyncOpenAI()` does not read openai.api_key, set at line 56) is a real
problem; the run's attempt 3 fixed it. With the new findings in its prompt, test_openai.py
would have been accepted in attempt 3, so the job would have reached verification instead of
stopping after attempt 7.

The run's final tree (fix.diff) under the new scanner: the accepted versions of azure_openai.py
(85), azure_openai_chat.py (91), openai.py (70) and openai_chat.py (79) still read
`model_outputs.choices[0]["text"]` / `["message"]["content"]` (the model kept them after the
rejections), and tests/llms/test_openai_chat.py:35 still sets dict mock data. HEAD's scanner
saw none of these, so a verification would have missed code that raises `TypeError: object is
not subscriptable` on 1.x (the tests' dict mocks hide it); now the completeness check reports
them as remaining findings.

Replies were written without the new findings in their prompts, so this shows what the checks
accept, not what the model would have sent.

Baseline on `~/apifix-targets/llmflows` (real sandbox, Python 3.11, pip cache on): the install
now succeeds on the first attempt (about 40 s), from requirements.txt with its pins applied as
the lockfile, and the repo installs editable. The plan note: "pyproject.toml pins
pinecone-client==2.2.4, tiktoken==0.5.1 where requirements.txt pins pinecone-client==2.2.1,
tiktoken==0.3.3: installed from requirements.txt (the repo's CI installs it:
.github/workflows/pylint.yml), not pyproject.toml". 47 tests: 46 pass, 1 fails before any
change: `tests/llms/test_claude_chat.py::TestClaudeChat::test_generate`, `TypeError:
Client.__init__() got an unexpected keyword argument 'proxies'` (anthropic 0.3.11 with httpx
0.28; requirements.txt does not pin httpx). It is a no-signal test for openai-v1.

The first two tries failed on PyPI read timeouts (about 6-12 kB/s that day) while downloading
hf_xet (4.2 MB, via tokenizers -> huggingface-hub), and each was retried once as transient, as
intended. That wheel was then put into the pip cache by a separate `pip download --timeout 120`
in python:3.11-slim, and the third try installed from the cache.
