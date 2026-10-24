"""Shows a suggestion for an error; failing to get one must never break the caller."""

from helper.generate import suggest


def on_error(error: str) -> str:
    try:
        return "Suggestion: " + suggest(error)
    except Exception as e:
        print("Error while trying to provide a suggestion:", e)
        return ""
