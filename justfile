# agent-mgr -- task runner.

# Run the contract suite. It invokes the real scripts rather than grepping their
# source: a test that re-implements the command under test asserts nothing about
# the command.
test:
    uv run --no-project --python 3.13 --with pytest==8.4.2 --with pyyaml==6.0.2 pytest -q
