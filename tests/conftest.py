import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def registry(tmp_path):
    """An isolated registry file; never the operator's real one."""
    return tmp_path / "config" / "agent-mgr" / "agents"


@pytest.fixture
def run(registry, tmp_path):
    """Invoke the real agent-mgr CLI with an isolated registry and HOME."""

    def _run(*args, env=None, check=False):
        e = dict(os.environ)
        e["AGENT_MGR_REGISTRY"] = str(registry)
        e["HOME"] = str(tmp_path / "home")
        (tmp_path / "home").mkdir(exist_ok=True)
        if env:
            e.update(env)
        return subprocess.run(
            [str(ROOT / "agent-mgr"), *args],
            capture_output=True, text=True, env=e, check=check,
        )

    return _run


@pytest.fixture
def instance(tmp_path):
    """Create an instance repo on disk and return its path."""

    def _instance(name, descriptor="", config="model:\n  provider: openai-codex\n"):
        repo = tmp_path / f"{name}-repo"
        repo.mkdir(exist_ok=True)
        (repo / "agent.env").write_text(descriptor)
        if config is not None:
            (repo / "config.yaml").write_text(config)
        return repo

    return _instance
