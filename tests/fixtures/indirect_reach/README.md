# indirect_reach

Synthetic fixture for apifix (openai 0.x), modelled on genai: a notebook extension whose
test reaches the openai code only indirectly. `tests/test_context.py` imports nothing but
`notebook.context`, which never touches openai; importing it runs `notebook/__init__.py`,
which imports `notebook.magics`, which imports `notebook.generate`, the module that calls
openai. The test drives it through a cell magic (`shell.run_cell("%%assist ...")`, see
conftest.py), so the right mock target is in `notebook.generate`, not `notebook.context`.
`notebook/batch.py` also calls openai, but nothing the test imports reaches it.
