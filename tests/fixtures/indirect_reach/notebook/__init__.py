"""A notebook assistant. Loading the extension registers its cell magics."""

from notebook.magics import assist


def load_extension(shell):
    shell.register_magic("assist", assist)
