# PR text review fixes

## Request

Review of the aipl PR text (`~/apifix-runs/aipl-gptoss-2`: pr-body.md, fix.diff) before sending it
upstream found these bugs:

1. "Main changes" paired a finding with the wrong added line: `openai.api_base =
   os.environ['LLM_CLIENT_ENDPOINT']` -> `openai.base_url = "https://api.goose.ai/v1"` (the real new
   line keeps os.environ['LLM_CLIENT_ENDPOINT']). Match each finding to the added line that replaced
   it in the same hunk; if unsure, leave the bullet out.
2. Bullets say `client.chat.completions.create` / `client.embeddings.create` while the diff uses
   `openai.chat.completions.create` / `openai.embeddings.create`. Name what the diff actually wrote.
3. The client_config finding on a kept `openai.api_key = ...` assignment is a false alarm when the
   file's calls use the module-level client (which reads it). Count it as resolved in that case
   (scanner/verifier), so it no longer appears under "Please review by hand" or in "N of M resolved".
4. The weak-evidence section repeats the uncovered lines three times and says "threshold 50%". Show
   one plain sentence ("No test runs these changed lines; please read them carefully:") and the list
   once; drop the duplicate <details> block when that section is shown.
5. Comment lines the model added that are not in the original are reverted, even next to a finding
   (the commented-out-code check stays).
6. Add to the openai-v1 guide: change only what the migration needs; keep the original formatting
   and leave other code (e.g. bare `except:`) as it is.

Tests for each. Run the full suite once, then ruff. Commit and push to origin main.

## Plan

1. `rules._config_change` looks for a kept assignment only in the lines that replaced the finding
   (hunk); a client argument elsewhere in the file counts only if it is the only one.
2. `rules.pr_change` takes the receiver from the diff (`openai`, `self.client`); none found: no
   bullet. A mock-target string keeps just its last segment.
3. `python_checks._without_read_config`: a file whose only findings are kept-value client_config
   ones, that uses module-level resources (`openai.chat...`) and builds no client, has none.
4. `pr_body._weak_section` lists the uncovered hunks (or recorded spans) once under the plain
   sentence, drops the coverage-threshold reason and the "only N/M" line, and `_tests_section`
   skips the `<details>` block when the notice lists them.
5. `code_checks.drop_added_comments` removes comment-only lines the original lacks (multiset by
   text), except old lines commented out, which `commented_out_code` still reports. Called in
   `whole_file.check_candidate` right after the text reverts.
6. A paragraph in the always-sent client part of `guide.md`.

## Built

All six, with tests in test_pr_body, test_scanner, test_code_checks, test_whole_file and
test_migrations.
