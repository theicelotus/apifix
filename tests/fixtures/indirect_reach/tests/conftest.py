import pytest

import notebook


class Shell:
    """A minimal notebook shell: a history and cell magics."""

    def __init__(self):
        self.history = []
        self.magics = {}

    def register_magic(self, name, function):
        self.magics[name] = function

    def run_cell(self, cell):
        self.history.append({"role": "user", "content": cell})
        if cell.startswith("%%"):
            name, _, body = cell[2:].partition("\n")
            return self.magics[name](self, body)
        return None


@pytest.fixture
def shell():
    s = Shell()
    notebook.load_extension(s)
    return s
