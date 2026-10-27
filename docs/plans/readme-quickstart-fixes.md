# Plan: README fixes before publishing

## Request

Three README fixes before publishing (docs only):

1. In "Results on real repos", define weak evidence correctly: "the tests pass but run less than
   half of the changed lines, so the PR lists those lines for review".
2. Quick start: start with `git clone https://github.com/theicelotus/apifix.git && cd apifix`.
3. Quick start must work for someone with only a hosted-provider key. Check what apifix does when the
   model has no price entry. If a run needs prices.toml, choose the simplest honest option (e.g.
   --no-cost-limit with a one-line explanation, or a short prices.toml step) and show it.

Run the README-reading tests and ruff, then commit.

## Plan

- Weak-evidence wording matches `min_changed_line_coverage = 0.5` in `verifier.py`.
- Without a price entry, `fix` raises "no price for model ..." unless `--no-cost-limit` is given
  (`agent.py`, `run()`). The old Quick start pointed at a `prices.toml` that does not exist.
  Use `--no-cost-limit` plus a short explanation; the prices.toml route stays documented in
  "Fast testing with a hosted free tier".

## Built

README.md only: the three edits above.
