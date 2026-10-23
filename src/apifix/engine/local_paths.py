"""Keep the local user out of written outputs: home directory -> `~`, pytest's per-user temp
dir `pytest-of-<user>` -> `pytest-of-USER`.

Trajectories and reports are often published as evidence; absolute paths of the run's host
(the repo, the pip cache, temp dirs) would carry the user name with them. Only whole path
prefixes are replaced: `/home/al` in `/home/alice` or `/x/home/al/y` is left alone.
"""

from __future__ import annotations

import getpass
import re
from pathlib import Path
from typing import Any


def _user() -> str | None:
    try:
        return getpass.getuser()
    except Exception:  # no login name (containers without a passwd entry)
        return None


def _patterns(home: str | None, user: str | None) -> list[tuple[re.Pattern[str], str]]:
    patterns = []
    home = (home or "").rstrip("/")
    if home:  # never for a home of "/"
        patterns.append((re.compile(r"(?<![\w./-])" + re.escape(home) + r"(?=/|$|[^\w.-])"), "~"))
    if user:
        patterns.append(
            (re.compile(r"/pytest-of-" + re.escape(user) + r"(?=/|$)"), "/pytest-of-USER")
        )
    return patterns


def redact_local_paths(value: Any, *, home: str | None = None, user: str | None = None) -> Any:
    """`value` with the home dir and user name replaced in every string (also dict keys) of
    nested dicts, lists and tuples. Defaults: `Path.home()` and the login name."""
    if home is None:
        try:
            home = str(Path.home())
        except RuntimeError:  # no home directory
            home = ""
    patterns = _patterns(home, _user() if user is None else user)
    if not patterns:
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            for pattern, replacement in patterns:
                item = pattern.sub(replacement, item)
            return item
        if isinstance(item, dict):
            return {walk(k): walk(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return type(item)(walk(v) for v in item)
        return item

    return walk(value)
