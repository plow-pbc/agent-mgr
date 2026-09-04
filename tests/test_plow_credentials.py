"""materialize_plow_credentials: the plow-init credential file agent-mgr
writes from an agent's own home dotenv.

Not the latch/activation credentials in test_credentials.py -- this is the
PLOW_API_BASE/PLOW_AGENT_TOKEN pair the base image's plow-init oneshot reads
from a bind-mounted file, not from the container's environment.
"""
import stat

import pytest
from conftest import ROOT

from agent_mgr.deploy import materialize_plow_credentials
from agent_mgr.descriptor import resolve_agent
from agent_mgr.errors import AgentMgrError
from agent_mgr.registry import Registry


def _agent(run, instance, registry, tmp_path, monkeypatch, name="rowan", dotenv=None):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run("register", name, str(instance(name)), check=True)
    home = tmp_path / "home" / f".hermes-{name}"
    home.mkdir(parents=True, exist_ok=True)
    if dotenv is not None:
        (home / ".env").write_text(dotenv)
    return resolve_agent(name, Registry(registry), ROOT)


def test_materialize_writes_exactly_the_two_credential_keys_outside_the_home(
    run, instance, registry, tmp_path, monkeypatch
):
    agent = _agent(
        run, instance, registry, tmp_path, monkeypatch,
        dotenv="PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
               "AGENT_TZ=America/Chicago\n",
    )

    path = materialize_plow_credentials(agent, Registry(registry))

    assert path == agent.credentials
    assert path.parent == agent.home.parent, "must land outside the agent's own home"
    assert not path.is_relative_to(agent.home)
    text = path.read_text()
    assert "PLOW_API_BASE=https://api.plow.co" in text
    assert "PLOW_AGENT_TOKEN=tok_secret" in text
    assert "AGENT_TZ" not in text, "only the two credential keys belong in this file"
    assert "America/Chicago" not in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "dotenv, missing",
    [
        ("PLOW_AGENT_TOKEN=tok_secret\n", "PLOW_API_BASE"),
        ("PLOW_API_BASE=https://api.plow.co\n", "PLOW_AGENT_TOKEN"),
        ("", "PLOW_API_BASE"),
        ("PLOW_API_BASE=\nPLOW_AGENT_TOKEN=tok_secret\n", "PLOW_API_BASE"),
    ],
    ids=["missing-base", "missing-token", "missing-both", "blank-base"],
)
def test_materialize_fails_loud_naming_the_missing_key_and_agent(
    run, instance, registry, tmp_path, monkeypatch, dotenv, missing
):
    agent = _agent(run, instance, registry, tmp_path, monkeypatch, dotenv=dotenv)

    with pytest.raises(AgentMgrError) as excinfo:
        materialize_plow_credentials(agent, Registry(registry))

    assert missing in str(excinfo.value)
    assert agent.name in str(excinfo.value)
    assert not agent.credentials.exists(), "a partial file is worse than none"


def test_materialize_not_required_skips_silently_instead_of_raising(
    run, instance, registry, tmp_path, monkeypatch
):
    """A never-activated home has no real values yet -- deploy() and
    reload_if_running() pass required=False so that case is a silent no-op,
    not the failure test_materialize_fails_loud_... asserts for every other
    caller. Missing dotenv and missing keys both take this path."""
    agent = _agent(run, instance, registry, tmp_path, monkeypatch, dotenv=None)
    assert materialize_plow_credentials(agent, Registry(registry), required=False) is None
    assert not agent.credentials.exists()

    (agent.home / ".env").write_text("PLOW_AGENT_TOKEN=tok_secret\n")
    assert materialize_plow_credentials(agent, Registry(registry), required=False) is None
    assert not agent.credentials.exists()


def test_materialize_refuses_before_the_home_has_a_dotenv(
    run, instance, registry, tmp_path, monkeypatch
):
    """No 'agent-mgr deploy' has run yet -- same shape as migrate_plugin_env's
    own refusal, so the operator gets one consistent message for this class."""
    agent = _agent(run, instance, registry, tmp_path, monkeypatch, dotenv=None)

    with pytest.raises(AgentMgrError) as excinfo:
        materialize_plow_credentials(agent, Registry(registry))

    assert "deploy" in str(excinfo.value)
    assert not agent.credentials.exists()


def test_agent_credentials_env_matches_the_materialized_path(
    run, instance, registry, tmp_path, monkeypatch
):
    agent = _agent(
        run, instance, registry, tmp_path, monkeypatch,
        dotenv="PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n",
    )
    assert agent.environment()["AGENT_CREDENTIALS"] == str(agent.credentials)
    assert agent.environment()["AGENT_CREDENTIALS"] == str(
        materialize_plow_credentials(agent, Registry(registry))
    )
