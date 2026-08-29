# agent-mgr -- task runner.

# Run the contract suite. It invokes the real scripts rather than grepping their
# source: a test that re-implements the command under test asserts nothing about
# the command.
test:
    uv run --no-project --python 3.13 --with pytest==8.4.2 --with pyyaml==6.0.2 pytest -q

typecheck:
    uv run --no-project --python 3.13 --with mypy==1.17.1 mypy agent_mgr scripts/build_zipapp.py __main__.py
    uv run --no-project --python 3.13 --with mypy==1.17.1 mypy lib/fetch-tree

lint:
    uv run --no-project --python 3.13 --with ruff==0.12.11 ruff check agent_mgr scripts/build_zipapp.py __main__.py lib/fetch-tree
    uv run --no-project --python 3.13 --with ruff==0.12.11 ruff format --check agent_mgr scripts/build_zipapp.py __main__.py lib/fetch-tree

check: lint typecheck test

package:
    python3 scripts/build_zipapp.py dist/agent-mgr.pyz
    tmp=$(mktemp -d); HOME="$tmp" AGENT_MGR_REGISTRY="$tmp/agents" dist/agent-mgr.pyz --json ls
