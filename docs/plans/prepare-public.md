# Prepare the repo to go public

Requested 2026-10-27. Nothing is pushed in this step. The request is verbatim.

## Request

Prepare the repo to go public. Don't push anything.

1. LICENSE: MIT, "Copyright (c) 2026 theicelotus" (handle only, no real
   name). Set the license in pyproject.toml.

2. Replace the top of README.md, from the title down to the first
   detailed section, with the text below. Keep every existing section
   after it. Check each number against the "Live results" table and fix
   any that don't match:

   (new title, tagline, pitch, "Results on real repos", "How it works" and a "Quick start" of
   under 15 lines written from the existing install and usage docs: install, a `fix` run with
   a hosted free tier, then `apifix pr`.)

3. Check the whole repo is ready to publish: no home paths, usernames,
   real names or emails, keys, or the hosted provider org IDs in any tracked file; every
   link in README.md points to a file that exists. Report anything found.

Commit when ruff and the tests that read README.md pass.

## Plan

1. `LICENSE` (MIT), `license`, `license-files` and `authors` in `pyproject.toml`; hatchling
   >= 1.27 for the SPDX `license` string.
2. New README top. The old top had no headings: a status paragraph, a command list and several
   paragraphs of detailed docs. Replace the status paragraph; keep the command list under
   "## Commands" and the paragraphs after it unchanged.
3. Count the Live results table: 7 real repos (plus the fixture row), 4 `verified`, 3
   `verified_weak`, no already-migrated row. Fix the draft's numbers to match.
4. Scan tracked files for home paths, usernames, names, emails, keys and the hosted provider org IDs; check
   every README link and path.

## Built

- `LICENSE`; `pyproject.toml` license fields.
- README top as above, with "7 open-source repos" (the draft said 8) and without the "1
  recognised as already migrated" (no row in the table: it was a scan-only case, Athena, see
  docs/plans/already-migrated.md) and without "current apifix" (the runs span several commits).
- Scan result: nothing to fix. Only placeholders and test fakes match (`1234+pat@users...`,
  `sk-apifix-...-not-a-real-key`, `/home/al`); the `~/apifix-targets` paths in published
  files are already redacted; the hosted provider org IDs in the published llmflows run are `org_<redacted>`;
  the only commit identity is the noreply address. All README links resolve.
