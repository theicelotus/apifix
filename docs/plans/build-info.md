# Plan: record apifix's version and commit in runs

## Request

Record apifix's version and git commit (with "-dirty" if the tree has
uncommitted changes) in trajectory.json's job section and in report.md,
so a published run names the commit it used. Tests, a short plan in
docs/plans/, commit.

## Plan

- New `engine/build_info.py`: `apifix_build()` returns version, full commit, dirty flag (via
  `git -C <package dir>`; None outside a git checkout); `build_label()` renders it.
- `job.apifix` in trajectory.json and an `- apifix:` line in report.md, for both `fix`
  (agent.py) and `verify` (verify_diff.py).
- Tests: tests/test_build_info.py plus assertions in test_agent.py and test_verify_diff.py.

## Built

As planned. Dirty means `git status --porcelain` is non-empty (untracked files count).
