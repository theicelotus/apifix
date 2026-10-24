"""The %%assist cell magic."""

from cellhelp.generate import suggest


def assist(cell: str) -> str:
    return suggest(cell)
