Synthetic fixture (shape of llmflows run 3): a 0.x module that passes `engine=` / `deployment_id=`
to openai through a retry wrapper, and tests that patch the openai method and assert those
kwargs (`assert_called_once_with(engine=...)`, `call_args.kwargs["engine"]`) several lines away
from the `@patch` line. Migrating the calls to `model=` means changing those assertions too.
