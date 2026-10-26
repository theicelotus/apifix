# End-to-end test of `apifix pr --push` on a repo you own

One real run of the whole path (push, draft PR) against `theicelotus/apifix-pr-sandbox`, a
**private** repo that holds only a synthetic openai 0.28 support bot
(`tests/fixtures/pr_sandbox/`). You own the upstream, so there is no fork: the PR goes from
`apifix/openai-v1` to the repo's own `main`. Nothing touches any other repository: apifix
refuses a `gh` login other than `--github-user`.

Prepared but not run: nothing below has been executed against GitHub. `~/apifix-pr-sandbox`
(one local commit on `main`, authored as your noreply identity, no remote) already exists.
To redo that step: `.venv/bin/python scripts/prepare_pr_sandbox.py [DEST]`.

## Before you start

```bash
cd ~/Downloads/Project/apifix
git status --short          # must print nothing: a dirty apifix build is refused with --push
gh auth status              # must say "Logged in to github.com account theicelotus"
docker info > /dev/null && echo docker ok
echo "$APIFIX_GIT_NAME <$APIFIX_GIT_EMAIL>"    # your noreply identity
git -C ~/apifix-pr-sandbox log --format='%an <%ae>'   # the same identity, one commit
```

`apifix fix` also needs an LLM key (`ANTHROPIC_API_KEY`, or the `--provider openai-compat`
options you use elsewhere). The code is synthetic, so any provider is fine; expect well under $1.

## 1. Create the private repo and push `main`

```bash
cd ~/apifix-pr-sandbox
gh repo create theicelotus/apifix-pr-sandbox --private \
  --description "Synthetic openai 0.28 project for testing apifix pr"
git remote add origin https://github.com/theicelotus/apifix-pr-sandbox.git
git -c credential.helper= -c credential.helper='!gh auth git-credential' push -u origin main
gh repo view theicelotus/apifix-pr-sandbox --json visibility --jq .visibility   # PRIVATE
```

## 2. Fix a clone

The run must be made on a clone of what is on GitHub, so its base commit is there.

```bash
gh repo clone theicelotus/apifix-pr-sandbox ~/apifix-pr-sandbox-clone
cd ~/Downloads/Project/apifix
.venv/bin/apifix fix ~/apifix-pr-sandbox-clone --migration openai-v1 \
  --out ~/apifix-runs/pr-sandbox
jq -r .verdict ~/apifix-runs/pr-sandbox/verification.json    # verified or verified_weak
```

Anything else: stop and read `~/apifix-runs/pr-sandbox/report.md`. `--out` is outside the apifix
checkout on purpose, so the run does not make apifix's own tree dirty.

## 3. Dry run, read the PR text, then push

```bash
.venv/bin/apifix pr ~/apifix-runs/pr-sandbox --repo ~/apifix-pr-sandbox-clone
less ~/apifix-runs/pr-sandbox/pr-body.md          # what a maintainer would read
git -C ~/apifix-pr-sandbox-clone log -1 --format='%an <%ae> | %cn <%ce>' apifix/openai-v1
```

The author and committer must be your noreply identity. Edit `pr-title.txt` / `pr-body.md` if you
like: that is what gets sent. Then, with the same arguments plus `--push` (it reuses the branch
the dry run made):

```bash
.venv/bin/apifix pr ~/apifix-runs/pr-sandbox --repo ~/apifix-pr-sandbox-clone --push \
  --github-user theicelotus --upstream theicelotus/apifix-pr-sandbox
```

Expect: `GitHub account: theicelotus`, `pushed apifix/openai-v1 to theicelotus/apifix-pr-sandbox`,
`opened a draft pull request: https://github.com/theicelotus/apifix-pr-sandbox/pull/1`. The
account and URL are also in `~/apifix-runs/pr-sandbox/pr.json` (no token in it).

If it says the repo does not allow draft pull requests (GitHub Free allows drafts only in public
repos), run the same command again with `--ready` added. Nothing is lost: the pushed branch is
reused. Any other failure says how far it got; running the same command again continues.

## 4. Look at it

```bash
gh pr view "$(jq -r .pr_url ~/apifix-runs/pr-sandbox/pr.json)" --web
```

Check the body renders (the table, the `<details>` blocks), the commit is authored by your
noreply identity, and the PR is from `theicelotus:apifix/openai-v1` into `main`.

## 5. Close the PR and delete the branch

```bash
gh pr close "$(jq -r .pr_url ~/apifix-runs/pr-sandbox/pr.json)" \
  --comment "apifix end-to-end test; closing."
gh api -X DELETE repos/theicelotus/apifix-pr-sandbox/git/refs/heads/apifix/openai-v1
git -C ~/apifix-pr-sandbox-clone branch -D apifix/openai-v1
gh pr list --repo theicelotus/apifix-pr-sandbox --state all      # one PR, CLOSED
gh api repos/theicelotus/apifix-pr-sandbox/branches --jq '.[].name'   # main only
```

To run it again: do step 5, then `rm -rf ~/apifix-runs/pr-sandbox` (or use another `--out`) and
start from step 2 (the old clone can stay; `git -C ~/apifix-pr-sandbox-clone status` must be clean).
When you are done with the repo: `gh repo delete theicelotus/apifix-pr-sandbox` (needs the
`delete_repo` scope: `gh auth refresh -s delete_repo`).
