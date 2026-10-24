"""The conversation so far, from a shell's history."""


def build_context(history):
    """Messages for the model: every history entry with content, oldest first."""
    return [{"role": m["role"], "content": m["content"]} for m in history if m["content"]]
