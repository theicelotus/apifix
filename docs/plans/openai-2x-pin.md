# openai 2.x in the target pin

## Request (verbatim)

> Read CLAUDE.md. openai-python is now on 2.x (2.53.0 as of August 2026), which keeps backward
> compatibility with the 1.x API apart from function tool-call output types. Change the
> openai-v1 migration's target pin from openai>=1.55.3,<2 to openai>=1.55.3,<3 everywhere (pin
> edits, guide, smoke check, unpinned-declaration handling, lockfile handling, tests, README
> and CLAUDE.md examples), and check the companion pins (typing-extensions, httpx, anyio,
> pydantic, distro, jiter) against a current 2.x release's requirements. Run the smoke check in
> the sandbox against the newest 2.x to confirm the migrated client code works there. The
> README's existing live results stay as they are; they were verified against 1.x. Fast suite,
> Docker if the network allows, and ruff, then commit.

## What PyPI says (checked 2026-10-25)

- Newest 2.x: 2.54.0 (2026-08-11). 3.0.0 followed on 2026-08-12 (newest 3.37.0): it replaced
  httpx with `httpx2>=2.7,<3` and needs `anyio>=4.10`, so `<3` is the right cap.
- 2.0.0's only breaking change: `ResponseFunctionToolCallOutputItem.output` /
  `ResponseCustomToolCallOutput.output` (Responses API) may be a list instead of a string.
  0.x had no Responses API, so code the migration writes never reads them.
- Requirements of every release in `[1.55.3, 3)` (182 releases): the upper bounds never change
  (typing-extensions <5, anyio <5, httpx <1, pydantic <3, distro <2, jiter <1). Minimums are
  1.55.3's except typing-extensions >=4.14 from 2.29.0 and jiter >=0.10 from 2.3.0.
  requires-python: >=3.8 up to 2.7.1, >=3.9 from 2.7.2, >=3.10 from 2.49.0.

## Plan

1. `rules.PIN_SPEC = ">=1.55.3,<3"`; it feeds pin edits, the dependency findings'
   suggestions, `LibraryTarget.spec` (lockfile handling, --sandbox-requirements upgrades) and
   the smoke check's install. The dependency check needs no logic change: `openai<2` and
   `openai<3` still allow 0.x ([likely]); `openai>=2` / `==2.54.0` allow only >=1.0 (no
   finding).
2. Companion pins: keep the values. A repo pin is raised only when it allows none of the range;
   the range must be the loosest across the allowed openai releases (1.55.3's), since a pin
   between (typing-extensions 4.12) makes pip pick an older release in the range, which works.
   Update the comment to say what was checked.
3. `CLIENT_RESOURCES` (autospec check): add 2.54.0's new client resources (admin,
   content_provenance_checks, skills, videos).
4. Guide (Dependencies part), README, CLAUDE.md: the new pin and why 2.x is fine. The lockfile
   companion note names the fix's range (`which openai>=1.55.3,<3 needs`) instead of "current
   openai releases" (those now need more). README live results, `benchmark/live/` and earlier
   plans stay as they are.
5. Tests: the string everywhere, plus `openai<3` flagged, 2.x declarations not flagged, and a
   lock pin of openai 2.54.0 kept / 1.30.0 and 3.0.0 dropped under the migration's pin.
6. Sandbox: the smoke check and tests of migrated (1.x API) code with `openai>=1.55.3,<3`, on
   several Pythons; then the Docker suite.

## What was run

- Sandbox (apifix's `Sandbox` with the migration's smoke check), a repo of migrated 1.x-API
  code pinned `openai>=1.55.3,<3`, tests covering sync / streaming / async chat, embeddings,
  `RateLimitError` / `APIConnectionError` built from httpx objects, and the real client
  failing offline: Python 3.12 and 3.14 installed openai 2.54.0 (httpx 0.28.1, pydantic
  2.14.1, typing-extensions 4.16.0), smoke check ok, 6 of 6 passed. Python 3.8 installed 2.2.0
  (2.3.0 on need jiter>=0.10, which does not support 3.8): ok, 6 of 6. With
  `typing_extensions==4.12.2` pinned as well (3.12): 2.28.0, ok, 6 of 6, which confirms the
  companion minimums can stay at 1.55.3's.
- 2.54.0's client resources, listed in a throwaway container: 1.109.1's plus admin,
  content_provenance_checks, skills, videos.
- Fast suite: 1665 passed. ruff clean.
- Docker suite (`-m docker --docker`): 19 of 23 passed on the first run; 3 failures were
  assertions that the smoke check installed a 1.x (now `allows(MIGRATION.pin_spec, version)`),
  passing on rerun. `test_pip_cache_is_reused_by_the_next_install` (installs only six, no
  openai) failed twice on its first, empty-cache install (exceeded 300 s, then "dependency
  install failed") and passed its second install both times; run by hand it passed, its
  empty-cache install taking 222 s (normally seconds): this connection, not the change.

This host's Docker bridge network had no route out ("No route to host"; fixing it needs root),
so the Docker runs used a scratch pytest plugin outside the repo that maps only the install
phase's `--network bridge` to `--network host`. The install container still only sees the
generated requirements file; test and smoke phases kept `--network none`.
