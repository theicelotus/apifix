"""Shows a suggestion for a cell's error; failing to show one must never break the caller."""

from cellhelp import display
from cellhelp.generate import suggest


def on_error(error: str) -> None:
    try:
        display.show("Let's see how we can fix this...")
        display.show(suggest(error))
    except Exception as e:
        print("Error while trying to provide a suggestion: ", e)
