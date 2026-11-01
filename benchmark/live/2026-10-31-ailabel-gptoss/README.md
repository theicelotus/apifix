Code in `trajectory.json`, `fix.diff` and `report.md` comes from [thornewolf/ailabel](https://github.com/thornewolf/ailabel) (commit e2ad1c4), MIT-licensed: see `LICENSE.ailabel`.

Run with apifix commit 3f67723, migration `google-genai`, model `openai/gpt-oss-120b`. Verdict: **verified**, on attempt 1.

The tests pass before and after the fix. The sandbox set dummy key variables so the tests could import the module (the repo's own test setup does not set them), and the tests ran 59% of the changed lines (16 of 27). That is evidence the migrated code imports and the covered paths still behave; the other 41% of the changed lines are not exercised by any test and were not reviewed by hand.
