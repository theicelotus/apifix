# Plan: open a pull request for a verified fix (milestone 2)

## Request

Milestone 2: apifix opens a pull request for a verified fix.
Write the plan only: docs/plans/pull-requests.md with this request
verbatim, then stop for my approval. Don't build anything yet. Keep the
plan under ~150 lines, and read only the code you need to plan this.

Split the plan into two parts that can be built separately:

2a (dry run, no GitHub):
- Take a finished run's output dir (fix.diff, report.md,
  verification.json, trajectory.json) and a clean checkout of the target
  repo. Make a local branch with one commit holding the fix, and write
  pr-title.txt and pr-body.md. Push nothing.
- Only for verdict verified or verified_weak; refuse anything else and
  say why.
- Commit identity: use the configured noreply identity, never the
  machine's default git email. Refuse to commit if it isn't set.
- The PR body, for a maintainer who has never heard of apifix: what
  changed and why (openai 0.x -> 1.x), tests before and after,
  changed-line coverage, findings resolved, the review-by-hand list,
  lockfiles to regenerate, the sandbox setup (no network), the apifix
  version and commit, and how to re-check it (apifix verify). Say
  plainly that an AI model wrote the code and apifix verified it.
  verified_weak must say up front what the tests didn't cover. Short
  enough that a maintainer will read it.

2b (GitHub):
- Fork flow: fork, push the branch to the fork, open the PR upstream.
  Compare the gh CLI with the GitHub REST API (token from the
  environment) and recommend one. A token must never appear in output,
  logs or trajectory files.
- Pushing and opening a PR needs an explicit flag.
- How to test end to end against a fork the user owns (a PR from a
  branch to the fork's own main) before any real upstream PR.

Also include: the CLI shape (a new `apifix pr <run dir>` command or a
flag on fix: pick one and say why), files and modules, tests, what's out
of scope (GitHub App, scheduled PRs), and open questions for me.

## CLI shape: a new command

`apifix pr <run dir> --repo <checkout> [--push] [--upstream OWNER/REPO]`. Not a flag on `fix`:
a PR is a separate decision made after a person reads report.md; `fix` costs money and minutes,
and `pr` must be re-runnable (new body wording, a failed push) without re-running the LLM. It also
works for `apifix verify` runs (a hand-written diff). Exit 0 = branch made (and PR opened with
`--push`), 2 = refused, with the reason on stderr.

## 2a: local branch + PR text (no network)

**Gap found:** trajectory.json records the repo *path* only, not its commit, and nothing ties
fix.diff to what was verified. Both `fix` and `verify` add to `job`: `repo_commit` (HEAD sha,
`null` outside git, plus `repo_dirty`) and `diff_sha256` (of fix.diff as written).

Checks before touching git, each refusing with a one-line reason:
1. Run dir has all four files; `verification.json` verdict and `trajectory.json` outcome agree and
   are `verified`/`verified_weak`. Anything else: "verdict is rejected: <blocking reason codes>".
2. sha256(fix.diff) == `job.diff_sha256` (the diff was not edited after verification).
3. Checkout: a git work tree with no uncommitted changes, `job.repo_commit` present in it, run not
   made from a dirty tree. Old runs without `repo_commit`: refuse (see open questions).
4. Identity: `APIFIX_GIT_NAME` and `APIFIX_GIT_EMAIL` set, email ends in
   `@users.noreply.github.com`. Otherwise refuse; never fall back to git config.
5. Branch `apifix/openai-v1` (migration name) does not exist yet; refuse rather than overwrite.

Building the commit without touching the user's working tree or current branch: temporary index
(`GIT_INDEX_FILE`), `git read-tree <base>`, `git apply --cached` the diff (byte-exact, CRLF kept;
same symlink refusal as `verify_diff.apply_diff`), `git write-tree`, `git commit-tree -p <base>`,
`git update-ref refs/heads/<branch> <sha> ""` (fails if the branch appeared meanwhile). Every git
call runs with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, author and committer from
the env identity, `core.hooksPath=/dev/null`, `commit.gpgSign=false`, so the machine's identity,
hooks and global config can't leak in. Afterwards: the commit's changed files equal the diff's.

Commit: `Migrate to the openai 1.x API`, body naming the model, apifix version/commit, verdict.
Outputs in the run dir, redacted: `pr-title.txt`, `pr-body.md`, `pr.json` (branch, shas, PR URL).

**PR body** (new `pr_body.py`, built from verification.json + trajectory.json, not from report.md,
which is written for apifix users). Target: ~40 rendered lines; long lists truncated to 10 with
"and N more" and the rest in a `<details>` block. Order:
1. "This PR moves the code from the openai 0.x API to 1.x. An AI model (<provider>/<model>) wrote
   it and apifix, an automated migration tool, verified it; review it like any contribution."
2. verified_weak only, before anything else: "**What the tests did not cover**": the weak reasons
   (e.g. changed-line coverage 3/12) and the review-by-hand hunks, file:line.
3. Why: openai 1.0 removed the module-level API (`openai.ChatCompletion.create`, `openai.error`,
   ...); the 0.x calls fail on openai>=1. What changed: files changed; findings resolved
   (`findings_before - len(remaining_findings)` of `findings_before`); dependency pins set.
4. Tests: a 2-row table baseline vs fixed (passed/failed/errors/skipped from `runs.*.counts`),
   tests fixed by the change, new tests; changed lines executed by tests: x/y.
5. Review by hand (verified only if non-empty; for verified_weak it is already in 2) and
   `review_findings`.
6. Lockfiles to regenerate: pin notes `not updated: <lock>; regenerate it` and lockfile reasons.
7. How it was checked: Docker, Python <ver>, dependencies installed from a generated requirements
   file (the only networked step, no repo code), tests run with no network and no secrets
   (placeholder env vars named); baseline on the unmodified code.
8. Re-check: `git diff <base>..HEAD > fix.diff && apifix verify . --migration openai-v1 --diff
   fix.diff`, with the apifix version + commit that made the PR.

## 2b: GitHub

**gh CLI vs REST API.** REST via stdlib `urllib` needs no new tool and is what a future GitHub App
uses, but apifix must then hold the token, poll the async fork endpoint, and, hardest, give
`git push` credentials without the token reaching argv, remote config or git's error output.
gh does fork (`gh repo fork --clone=false`, waits for it), PR creation and git credentials
(`gh auth git-credential`) with the token kept in gh's keyring or `GH_TOKEN`; apifix never reads
it. **Recommend gh** for this milestone, behind a small `github.py` interface (`whoami`, `fork`,
`push`, `find_pr`, `open_pr`) that a REST/App backend can replace later.

Token safety: apifix never reads or passes a token; gh/git output goes through a scrubber
(`gh[opsu]_…`, `github_pat_…`, `x-access-token:…@`, `Authorization:` lines) before it is printed
or written; `pr.json` holds only names, shas and the PR URL; nothing goes into trajectory.json.

Flow with `--push` (without it, 2a only): `gh auth status` -> resolve upstream (`--upstream`, else
the checkout's `origin` if on github.com) -> check the base sha exists upstream -> refuse if a PR
from `<user>:<branch>` is already open -> fork (skipped when the user owns upstream) -> `git push
https://github.com/<fork>.git <branch>` with `-c credential.helper=` `-c credential.helper='!gh
auth git-credential'` (no remote added to the checkout) -> `gh pr create --repo <upstream> --head
<user>:<branch> --base <default branch> --title-file/--body-file` -> URL into `pr.json`.

**End-to-end test on your own repo:** push a small openai 0.x project with tests (from
tests/fixtures) to `<you>/apifix-pr-sandbox`; `apifix fix` a clone; `apifix pr <run> --repo
<clone> --push --upstream <you>/apifix-pr-sandbox`. You own upstream, so no fork: the PR goes from
`apifix/openai-v1` to its own main. Read the rendered body, close the PR, delete the branch. Then
an opt-in `-m github` test (`--github`, like `--llm`) against that repo; never run by default.

## Files

2a: `engine/pull_request.py` (check the run, build the commit, write outputs), `engine/pr_body.py`
(title, body, commit message), `engine/gitops.py` (isolated git runner, also used by 2b);
`agent.py`/`verify_diff.py` (`repo_commit`, `repo_dirty`, `diff_sha256`); `cli.py` (`pr`); README,
docs/notes.md. 2b: `engine/github.py` (gh wrapper, token scrubber), `--push` in `cli.py`.

## Tests

- `test_pull_request.py` (temp git repo + synthesized run dir): one refusal test per check above;
  author/committer are the env identity despite a fake `$HOME/.gitconfig` with another email;
  working tree, index, current branch untouched; tree == base + diff (CRLF byte-exact); no hook runs.
- `test_pr_body.py`: AI disclosure first; verified_weak section comes before "Why"; lockfile and
  review lists; truncation; line budget; no home path or username.
- `test_github.py`: a fake `gh`/`git` on PATH recording argv + env: no `--push` = no calls;
  call order; own-repo flow skips the fork; a fake token in `GH_TOKEN` never appears in argv,
  stdout, stderr or any written file; scrubber unit tests. `test_cli.py`: `pr` exit codes.

## Out of scope

GitHub App, scheduled or batch PRs, updating/rebasing an open PR, watching CI or answering
review comments, non-GitHub hosts, any web service.

## Open questions

1. Identity from env (`APIFIX_GIT_NAME`/`APIFIX_GIT_EMAIL`), or from `git config --local` in the
   checkout? And strictly require `@users.noreply.github.com`?
2. Runs made before `repo_commit` existed: refuse (re-run `apifix verify`, no LLM), or accept
   `--base <sha>`? I recommend refuse.
3. Refuse a run made with a dirty apifix build (`-dirty` commit), or only say so in the body?
4. Open the PR as a draft by default? I lean draft for the first real upstream PRs.
5. Sandbox repo name for the end-to-end test; may the live test open/close PRs there unattended?

## Approval amendments

Approved with these amendments (verbatim):

1. Identity from APIFIX_GIT_NAME / APIFIX_GIT_EMAIL; refuse unless the
   email ends in @users.noreply.github.com.
2. Runs without repo_commit: refuse, and say to re-run apifix verify
   (no LLM needed).
3. A run made with a dirty apifix build: allowed for the dry run with a
   line in the PR body; refused with --push (2b).
4. 2b: open PRs as drafts by default; --ready opens one ready for review.
5. Sandbox repo for the end-to-end test: theicelotus/apifix-pr-sandbox.
   The opt-in live test may open and close PRs there unattended, and
   must refuse any other upstream.
6. 2b: --push needs the expected GitHub login (--github-user or
   APIFIX_GITHUB_USER) and refuses if gh is logged in as anyone else.
   Show the account in the output and in pr.json.
7. PR body: include the "re-check with apifix verify" section only when
   APIFIX_REPO_URL is set (apifix isn't public yet), linking to it.
   End with one line: feel free to close this if it isn't useful.
8. PR body: when a lockfile wasn't regenerated, say the repo's CI may
   fail until it is. Regenerating lockfiles is out of scope for now;
   list it under open questions for later.

Scope of this build: 2a only. 2b (items 4, 5, 6 and `--push`) is not built yet.

Added to Open questions (amendment 8): 6. Regenerating lockfiles (so the PR's CI passes) is out
of scope for now; revisit after 2b.

## Built (2a)

- `engine/gitops.py`: isolated git runner (no global/system config, hooks, signing or inherited
  identity/redirect variables), `repo_state(path)` -> (HEAD, dirty), `None`s unless `path` is a
  repo's top level.
- `fix` and `verify` trajectories: `job.repo_commit`, `job.repo_dirty`; `cli._write_outputs`
  adds `job.diff_sha256`. `verify_diff.check_no_symlinks` split out of `apply_diff`.
- `Migration.pr_why` (data per migration); title is "Migrate to the <target_name> API".
- `engine/pr_body.py`: title, body, commit message. Order: AI disclosure, verified_weak notice,
  Why, What changed, Tests (before/after table), review by hand, lockfiles (with the CI warning),
  how it was checked (+ apifix version/commit, dirty-build line), re-check section only with
  `APIFIX_REPO_URL`, closing "feel free to close this" line. Lists show 10, the rest folds into
  `<details>`.
- `engine/pull_request.py`: the checks (run dir complete; verdict/outcome agree and are
  verified/verified_weak; fix.diff sha256; `repo_commit` and clean-tree records, with "re-run
  apifix verify (no LLM needed)"; clean checkout containing the commit; env identity ending in
  @users.noreply.github.com; branch absent), the commit (temp index, `update-ref` create-only,
  changed files == the diff's), and `pr-title.txt`, `pr-body.md`, `pr.json` (redacted).
  A dirty apifix build is allowed, noted in the body and `pr.json` (`apifix_dirty`) for 2b
  to refuse with `--push`.
- `apifix pr RUN_DIR --repo CHECKOUT` (exit 0 branch made, 2 refused). No `--push` yet.
- Tests: `tests/test_pull_request.py` (real temp git repos, real `verify_diff` with a fake
  sandbox), `tests/test_pr_body.py`.

Not built (2b): `--push`, `--ready`, `--github-user`, `github.py`, the sandbox-repo live test.

## Refinements

Request (verbatim):

> Refine the PR body (pr_body.py), using a real one as the example:
> ~/apifix-runs/wolverine-gptoss-pr/pr-body.md. Changes:
>
> 1. Opening line: "This PR was generated by apifix, an automated
>    migration tool (using gpt-oss-120b), and verified by running
>    the repo's tests before and after the change. Please review it like
>    any other contribution." <model> is the model name without a vendor
>    prefix like "openai/" (so it doesn't read as if OpenAI wrote it);
>    <host> is the endpoint's host (api.example.com), not apifix's provider
>    name. For runs that record no model (apifix verify), leave the
>    "using ..." part out.
> 2. Name the openai version the tests actually ran with (the fixed run's
>    installed version). If runs don't record it yet, record it in the
>    sandbox run summary. Say what the code moves to: "from openai 0.x to
>    the current API (tests ran with openai X.Y.Z)".
> 3. When test files were edited and no test-edit problem was found, say
>    so: "the tests' mocks and imports were updated to the new API;
>    apifix checked that no test or assertion was removed, skipped or
>    weakened". If there were test-edit findings, list them instead.
> 4. Add "Main changes": up to 5 bullets of old -> new API patterns, from
>    the resolved findings, deduplicated (e.g. `openai.ChatCompletion.create`
>    -> `client.chat.completions.create`; dict responses -> attributes).
> 5. When coverage is below 100%, list the changed lines no test ran
>    (file:line) in a collapsed <details> section, for verified too.
> 6. Pin line: add why, briefly: ">=1.55.3 (first release that works with
>    httpx 0.28), <3 (openai 3 changed its HTTP client)". Take this from
>    the migration's data, not hard-coded in pr_body.py.
> 7. Plural forms: "1 file", "3 files".
>
> Keep the body short. Update tests, and add a "Refinements" section to
> docs/plans/pull-requests.md with this request verbatim. Commit when the
> full suite and ruff pass.
>
> Then delete the branch apifix/openai-v1 in ~/apifix-targets/wolverine,
> re-run apifix pr on ~/apifix-runs/wolverine-gptoss-pr with --repo
> ~/apifix-targets/wolverine, and show the new pr-body.md.

### Plan

- Migration data (not pr_body.py): `pin_reasons` (clause -> reason, matched against the pin
  apifix wrote) and `pr_change(category, old_pattern)` (one "old -> new" bullet, or None).
  openai-v1 derives the call bullets from `RESOURCES`/`METHOD_RENAMES`/`KWARG_RENAMES`.
- Main changes: resolved findings = trajectory findings minus `remaining_findings` (matched by
  file, category, pattern, since lines shift); bullets deduplicated, most findings first, 5 max.
- Item 2: the fixed run already records the installed version (`runs.fixed.smoke.version`); no
  sandbox change needed. Left out when no smoke check ran.
- Item 5: verification.json gains `changed_line_coverage.uncovered` (file -> lines). Runs made
  before this have none, so their body shows the ratio only.
- Item 1: the body names the model only. The endpoint host (not provider name) goes in the
  commit message ("Written by: gpt-oss-120b via api.example.com") since the quoted opening line
  has no place for it.
- Test-edit findings = edits with severity other than `info`.

### Built (refinements)

- `pr_body.py`: new opening (tool, model without vendor prefix, checks), "from openai 0.x to the
  current API (tests ran with openai X.Y.Z)" from `runs.fixed.smoke.version` (already recorded,
  so no sandbox change), "Main changes", pin reasons, the uncovered-lines `<details>`, the
  test-edit sentence or list, singular/plural counts. The commit message names the model and
  the endpoint host ("Written by: gpt-oss-120b via api.example.com").
- Migration data: `Migration.pin_reasons` and `Migration.pr_change()` (openai-v1: `rules.py`).
- `verifier.py`: `changed_line_coverage.uncovered` (file -> lines) in verification.json.
- Tests: `test_pr_body.py`, `test_migrations.py`, `test_verifier.py`. Full suite 1874 passed.
- Known gap: runs made before this change have no `uncovered` lines, so their body shows the
  coverage ratio only until they are re-verified.

## PR-quality fixes (wolverine-gptoss-pr)

Request (verbatim):

> Two PR-quality fixes, found in ~/apifix-runs/wolverine-gptoss-pr:
>
> 1. pr-body "Main changes" must describe what this diff did, not the
>    finding type's generic pattern. wolverine's bullet says settings moved
>    to client arguments (api_key=..., base_url=...), but the fix dropped
>    `openai.api_key = os.getenv("OPENAI_API_KEY")` because OpenAI() reads
>    OPENAI_API_KEY itself, and the repo has no api_base. Build each bullet
>    from the resolved findings' actual old code and the fixed code, and
>    only name settings that appear in the diff.
>
> 2. The fix commented the line out instead of deleting it:
>    `# openai.api_key = ...  # Removed: OpenAI() reads the env variable itself`.
>    Add to the openai-v1 guide: delete replaced code, never leave it
>    commented out. Add a check in whole-file mode: a changed line that is
>    only the old code commented out (with or without an explanation) is a
>    check error asking to delete it.
>
> Tests, a short section in docs/plans/pull-requests.md, commit when the
> full suite and ruff pass.

### Plan

- `build_pull_request_text` takes the run's `fix.diff`. Each resolved finding is matched to the
  diff's run of changed lines that removed its old lines (old line numbers), and
  `Migration.pr_change(category, old_pattern, new_code, file_code)` builds the bullet from the
  old code and the lines the diff added there (`file_code`: all lines added to the file, since
  a setting moves to a client built elsewhere). A finding the diff did not touch gets no bullet.
- openai-v1 (`rules.pr_change`): a call bullet needs the new name in the added lines; a
  module setting is "→ client argument `api_key=...`" (the argument as the diff wrote it),
  "→ `openai.base_url = ...`", or "removed" (with "`OpenAI()` reads `OPENAI_API_KEY` itself"
  when the old value came from the variable 1.x reads); comment-only added lines never count.
  Response-access bullets show the old expression; error bullets name the class pair.
- Guide (`client` part): delete replaced code, never leave it as a comment.
- Check `commented_out` (`code_checks.commented_out_code`, run by `check_candidate` on Python
  files): a changed line that is only a removed original line behind `#`, optionally followed
  by `# explanation`, is an error naming the line and asking to delete it. Comments the original
  already had and comments that are not old code are not flagged.

### Built

- As planned. `apifix pr` passes the diff; runs made before this change need no new data.
- wolverine-gptoss-pr's body now reads: `openai.api_key = os.getenv("OPENAI_API_KEY")` removed:
  `OpenAI()` reads `OPENAI_API_KEY` itself (no `base_url`, no "client arguments").
- Tests: `test_pr_body.py`, `test_migrations.py`, `test_whole_file.py`, `test_agent.py`.
- Known gap: the whole-file check sees only a line that is exactly the old line behind `#`; old
  code reworded inside a comment, or a multi-line statement commented out as a block string, is
  not caught. Agent (tool-edit) mode has no such check yet.

## 2b build: `--push`

Request (verbatim):

> Build 2b from docs/plans/pull-requests.md, with approval amendments 3-6
> (drafts by default and --ready; --push refused for a dirty apifix
> build; the expected login via --github-user or APIFIX_GITHUB_USER,
> refused on a mismatch; the sandbox repo theicelotus/apifix-pr-sandbox).
>
> - engine/github.py: the gh wrapper and token scrubber as planned. Tests
>   use a fake gh/git on PATH only. Don't call real GitHub, and don't run
>   gh or git push against any real repo in this session.
> - Also fix the CLI output's "3 file(s)" plural.
>
> Then prepare the end-to-end test for me to run myself, without running
> it:
> - A small openai 0.x project with tests (from tests/fixtures), copied to
>   ~/apifix-pr-sandbox as a git repo whose commits use the noreply
>   identity from APIFIX_GIT_NAME/APIFIX_GIT_EMAIL, never my git config.
> - The exact commands for me: create theicelotus/apifix-pr-sandbox as a
>   PRIVATE repo with gh, push it, run apifix fix on a clone, run
>   apifix pr --push, then close the PR and delete the branch.
>
> Commit when the full suite and ruff pass.

### Plan

- `apifix pr RUN_DIR --repo CHECKOUT --push [--github-user LOGIN] [--upstream OWNER/REPO]
  [--ready]`. `--github-user` falls back to `APIFIX_GITHUB_USER`; `--push` without either is
  refused. `--upstream`, `--ready` and `--github-user` without `--push` are usage errors.
- Everything that can refuse runs before the local branch is made: dirty apifix build
  (`job.apifix.dirty`), expected login, `gh auth status`, `gh` login == expected login (case
  insensitive), upstream (flag, else the checkout's `origin` if it is github.com), the base commit
  exists upstream, no open PR from `<user>:<branch>`. `make_pull_request` gets `for_push` and a
  `preflight(plan)` hook called after the run's checks and before the commit is made.
- `engine/github.py`: `GitHub` wraps `gh` (`whoami`, `repo_info`, `commit_exists`, `fork`,
  `find_open_pr`, `open_pr`) and pushes with `git` (isolated, credential helper
  `!gh auth git-credential`, no remote added, never `--force`). apifix never reads a token;
  gh gets the caller's environment (`GH_HOST=github.com`, prompts off). Every string from gh or
  git is scrubbed before it is shown or stored (`gh[opsur]_…`, `github_pat_…`, `user:pass@`,
  `Authorization`/`Bearer`, and the literal values of `GH_TOKEN`/`GITHUB_TOKEN`).
- A fork is accepted only if its parent is the upstream. When the user owns upstream there is no
  fork and the PR goes from `<user>:<branch>` to the same repo's default branch.
- Drafts by default; `--ready` opens a ready PR. The PR title and body are the written
  `pr-title.txt` / `pr-body.md` (so a hand edit after the dry run is what gets sent).
- `pr.json` gains `github_user`, `upstream`, `fork`, `pushed`, `draft`, `pr_url`, written after
  each step (pushed, then PR opened). A failure after the branch exists says which steps happened
  and how to retry; there is no resume (a re-run refuses an existing branch, as in 2a).
- CLI: "1 file", "3 files" in the `pr` summary line.
- Tests: `tests/test_github.py` with fake `gh` and `git` executables as the only things on PATH.
- End-to-end test by hand: `tests/fixtures/pr_sandbox/` (a small openai 0.x project with tests),
  `scripts/prepare_pr_sandbox.py` copies it to `~/apifix-pr-sandbox` as a git repo with the env
  identity; the commands to run are in `docs/pr-sandbox.md`. Not run in this session.
- Not built: the opt-in `-m github` live test (amendment 5), a resume of a half-done push.

### Built (2b)

- `engine/github.py`: `GitHub` (gh: `whoami`, `repo_info`, `base_status`, `ensure_fork`,
  `find_open_pr`, `open_pr`; `git push` with `!gh auth git-credential`, never forced, no remote
  added), `scrub` (token patterns + the literal `GH_TOKEN`/`GITHUB_TOKEN` values), and
  `PullRequestPublisher` (`preflight` before the branch is made, `publish` after, pr.json updated
  after each step). Base-commit check is "ancestor of the default branch" (compare API), stricter
  than the planned "exists upstream", so the PR cannot carry other commits.
- `pull_request.make_pull_request(for_push=, preflight=)`: dirty apifix build refused with
  `--push` (amendment 3); a branch from an unfinished `--push` (or a dry run) is reused when
  pr.json, its parent, its tree (== base + fix.diff) and its author all match, so a failed push
  or PR creation is retried by running the same command (added: a draft PR fails in a private
  repo on GitHub Free, and the retry would otherwise be a non-fast-forward push).
  `update_summary` writes pr.json.
- `gitops.git`: `config=` and `timeout=`; trace variables (`GIT_TRACE*`, `GIT_CURL_VERBOSE`) are
  dropped from git's environment.
- CLI: `--push`, `--github-user` / `APIFIX_GITHUB_USER`, `--upstream`, `--ready` (drafts by
  default); the three options without `--push` are a usage error; the account is shown and in
  pr.json. "1 file" / "3 files" in the `pr` summary (the `(s)` forms in `scan`, `fix` and
  `cleanup` output are unchanged).
- By-hand end-to-end test, prepared and not run: `tests/fixtures/pr_sandbox/` (a support bot on
  openai 0.28.1 with 6 passing tests; hand-migrated it passes on openai 2.x),
  `scripts/prepare_pr_sandbox.py` (created `~/apifix-pr-sandbox`: one local commit on `main` as
  the env noreply identity, no remote), and the commands in `docs/pr-sandbox.md`.
- Tests: `tests/test_github.py` (fake `gh`/`git` from `tests/fake_github.py` are the only things
  on PATH), `tests/test_prepare_pr_sandbox.py`, plus the plural test in `test_pull_request.py`.
- Not built: the opt-in `-m github` live test (amendment 5, including refusing any other
  upstream); resuming is only by re-running the same command, there is no `--resume`.
- Known gaps: `gh repo fork` / `gh pr create` output formats and the fork's async creation were
  written from gh's documented behaviour and the fake, not run against GitHub (the end-to-end
  test is the first real run). Lockfile regeneration is still out of scope (open question 6).

## Small fixes after the first real PR (apifix-pr-sandbox#1)

### Request

1. PR title and commit subject: "Migrate from the openai 0.x API to the current one" (from the
   migration's data), since the tests run on 2.x.
2. `apifix pr --push` said "reused the branch of an unfinished push" when the branch came from a
   dry run. Say "reused the branch from the dry run" in that case.
3. Commit message for verify runs: "Written by: not recorded (apifix only checked this diff)"
   instead of naming `apifix verify`.
4. A request to a hosted API that hangs until --request-timeout should be treated like a network
   outage (the existing 30/60/120 s waits and retries), not end the run. Keep the current
   behaviour for local endpoints (localhost / Ollama).

### Plan

1. `pr_body.title_for` uses `migration.source_name`: "Migrate from the {source_name} API to the
   current one". No new migration field. Tests in `test_pr_body.py` / `test_pull_request.py`.
2. `PullRequestResult.from_dry_run`: set when a reused branch's pr.json has no `github_user`
   (only `publish` writes it, so a dry run never has one). The CLI prints "reused the branch from
   the dry run: " or keeps "reused the branch of an unfinished push: ".
3. `_written_by` text changed. Nothing else names `apifix verify` there.
4. Timeouts count as an outage on a hosted endpoint, with the same rules as a lost connection:
   waits of 30/60/120 s only after the endpoint has answered earlier in the job, same job note,
   same "apifix waited ... and tried again" ending on the final error. No quick retries first
   (a retry would hang as long as the first try). Hosted = a non-local host for the
   OpenAI-compatible provider; the Anthropic provider is always hosted (`APITimeoutError`). Local
   endpoints keep failing at once with the `--request-timeout` advice.

### Built

- `pr_body.title_for`: "Migrate from the {source_name} API to the current one" (the title,
  `pr-title.txt` and the commit subject); the verify-run line reads "Written by: not recorded
  (apifix only checked this diff)".
- `PullRequestResult.from_dry_run` (pr.json has no `github_user`); the CLI prints "reused the
  branch from the dry run: " in that case, "of an unfinished push" otherwise.
- `OpenAICompatLLM._call`: `RequestTimedOut` on a non-local host takes the 30/60/120 s outage
  waits (after the first answer; same job note) and ends with the "waited ... and tried again"
  suffix; local hosts and the first request fail at once as before. `AnthropicLLM`:
  `APITimeoutError` is a connection error for the waits.
- Tests: title and "Written by" in `test_pr_body.py` / `test_pull_request.py`; dry-run wording in
  `test_github.py`; hosted timeout waits, exhaustion, first-request and local cases in
  `test_llm_openai_compat.py`; the Claude timeout test in `test_llm.py` now expects the waits.
- Docs: `docs/notes.md` (timeouts and outages) and the README timeout line.
