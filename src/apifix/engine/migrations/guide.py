"""Migration guides split into parts, so a job's prompt carries only the parts its findings need.

A guide is Markdown with `<!-- part: <id> -->` marker lines. Each marker starts a part that runs
to the next marker; text before the first marker (the title) is always kept. A part `a.b`
belongs to section `a`: keeping it keeps `a` too, since that is where the heading is. The full
guide is the text without the markers.
"""

from __future__ import annotations

import re

MARKER = re.compile(r"^<!-- part: ([\w.-]+) -->\n", re.MULTILINE)
TITLE = ""  # id of the text before the first marker


def guide_parts(text: str) -> list[tuple[str, str]]:
    """(id, text) in order; the title first (id TITLE)."""
    pieces = MARKER.split(text)
    parts = [(TITLE, pieces[0])]
    parts += [(pieces[i], pieces[i + 1]) for i in range(1, len(pieces), 2)]
    return parts


def full_guide(text: str) -> str:
    return MARKER.sub("", text)


def with_sections(keep: set[str]) -> set[str]:
    """`keep` plus the section of every sub-part in it."""
    return keep | {part.split(".")[0] for part in keep}


def render_parts(parts: list[tuple[str, str]], keep: set[str]) -> str:
    """The title and the parts in `keep` (and their sections), in guide order."""
    keep = with_sections(keep) | {TITLE}
    text = ""
    for part, body in parts:
        if part not in keep:
            continue
        if body.startswith("#") and text:  # a section starts after a blank line
            text = text.rstrip("\n") + "\n\n"
        text += body
    return text.rstrip() + "\n"
