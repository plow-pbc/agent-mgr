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


def fake_docker(tmp_path, *, home, container="hermes-<name>", project="hermes-<name>",
                name="rowan", running=True, exec_output=None, log=None):
    """A `docker` that answers the three things agent-mgr asks of it.

    One builder rather than one per test file: every command now passes through
    resolve-guard, so every fake needs a parseable `config --format json` -- and
    three near-copies of that JSON drift the moment the guard reads a new field.

    `log` records argv when given, so a test can assert on what actually ran
    rather than on what the source says.
    """
    import json

    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    container = container.replace("<name>", name)
    project = project.replace("<name>", name)
    cfg = json.dumps({
        "name": project,
        "services": {"hermes": {
            "container_name": container,
            "volumes": [{"target": "/opt/data", "source": str(home)}],
        }},
    })
    parts = [
        "#!/usr/bin/env bash",
        f'printf "%s\\n" "$*" >> {log}' if log else "",
        'case "$*" in',
        f"  *\"config --format json\"*) cat <<'JSON'\n{cfg}\nJSON\n    ;;",
        f'  *"ps --status running --quiet"*) {"echo deadbeef" if running else ":"} ;;',
    ]
    if exec_output is not None:
        parts.append(f'  *exec*) echo {exec_output} ;;')
    parts += ["esac", "exit 0", ""]
    (b / "docker").write_text("\n".join(x for x in parts if x))
    (b / "docker").chmod(0o755)
    return b
