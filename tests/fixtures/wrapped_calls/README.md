Synthetic fixture: openai calls made through retry wrappers (`call_with_retry(func=..., **kwargs)`),
with 0.x per-call kwargs (`engine=`, `deployment_id=`, ...). `legacy.py` is 0.x code; `migrated.py`
is a half-done 1.x migration that still passes 0.x kwargs.
