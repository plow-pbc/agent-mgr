"""AGENT_BOOT_CONTRACT: the descriptor key that selects compose.yml (today's
shape) or compose.plow-init.yml (see that file). No agent renders from the
new shape until its own descriptor sets this -- these tests are the proof
that setting it, and only setting it, is what moves the needle.
"""
import os
from pathlib import Path

import pytest
from conftest import ROOT, fake_docker

from agent_mgr.descriptor import resolve_agent
from agent_mgr.local import SCRUB
from agent_mgr.registry import Registry


def _agent(run, instance, registry, tmp_path, monkeypatch, name="rowan", descriptor=""):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run("register", name, str(instance(name, descriptor=descriptor)), check=True)
    return resolve_agent(name, Registry(registry), ROOT)


@pytest.mark.parametrize(
    "descriptor, plow_init, target",
    [("", False, "/opt/data"), ("AGENT_BOOT_CONTRACT=plow-init\n", True, "/var/lib/hermes")],
    ids=["legacy", "plow-init"],
)
def test_the_boot_contract_selects_the_home_target_and_credentials_path(
    run, instance, registry, tmp_path, monkeypatch, descriptor, plow_init, target
):
    agent = _agent(run, instance, registry, tmp_path, monkeypatch, descriptor=descriptor)
    assert agent.plow_init is plow_init
    assert agent.home_mount_target == target
    assert agent.boot_contract == ("plow-init" if plow_init else "")
    assert agent.credentials == Path.home() / ".plow-credentials-rowan"
    assert not agent.credentials.is_relative_to(agent.home)
    # An agent repo's own compose.override.yml mounts a skill or SOUL.md
    # against this, not a hardcoded path, so it survives a contract change.
    assert agent.environment()["AGENT_HOME_TARGET"] == target


def test_credentials_land_outside_a_repo_declared_home_not_beside_it(
    run, instance, registry, tmp_path, monkeypatch
):
    """AGENT_HOME=<repo>/.hermes is supported, and for such an agent 'beside
    the home' is the repo's own checkout root -- exactly where deploy() would
    write a live bearer token for a routine `git add .` to stage. credentials
    must sit under the operator's real home regardless of where AGENT_HOME
    points."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo_hermes = tmp_path / "rowan-repo" / ".hermes"
    repo = instance(
        "rowan", descriptor=f"AGENT_HOME={repo_hermes}\nAGENT_BOOT_CONTRACT=plow-init\n"
    )
    run("register", "rowan", str(repo), check=True)
    agent = resolve_agent("rowan", Registry(registry), ROOT)
    assert agent.home == repo_hermes
    assert agent.credentials == Path.home() / ".plow-credentials-rowan"
    assert not agent.credentials.is_relative_to(repo)


def test_a_typo_in_the_boot_contract_is_refused_not_silently_kept_on_todays_shape(run, instance):
    """A typo here is the worst kind of bug: the agent looks opted in to
    whoever reads agent.env, but silently keeps running today's shape."""
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=Plow-Init\n")))
    r = run("resolve", "rowan")
    assert r.returncode != 0
    assert "AGENT_BOOT_CONTRACT" in r.stderr
    assert "Plow-Init" in r.stderr


def test_scrub_covers_every_key_environment_exports(run, instance, registry, tmp_path, monkeypatch):
    """A stale AGENT_BOOT_CONTRACT or AGENT_CREDENTIALS exported by an earlier
    agent-mgr invocation in the same shell must not leak into a different
    agent's compose call -- every key environment() sets needs a matching
    SCRUB entry, these two included."""
    agent = _agent(run, instance, registry, tmp_path, monkeypatch)
    assert set(agent.environment()) <= SCRUB


@pytest.mark.parametrize(
    "argv, materialized, allowed",
    [
        (("up",), False, False),
        (("up",), True, True),
        (("create",), False, False),
        (("start",), False, False),
        (("restart",), False, False),
        (("run", "--entrypoint", "/bin/true", "hermes"), False, False),
        (("down",), False, True),
        (("logs",), False, True),
    ],
    ids=["up-refused", "up-allowed", "create-refused", "start-refused",
         "restart-refused", "run-refused", "down-allowed", "logs-allowed"],
)
def test_the_credential_gate_covers_every_start_verb_and_only_those(
    run, instance, tmp_path, argv, materialized, allowed
):
    """One seam (compose() in local.py) gates every verb that creates or
    (re)starts a container -- up, create, start, restart, and run (which
    bypasses transition() entirely via LEAVES_RUNNING) -- against a missing
    credentials file, the failure that would otherwise bind-mount an empty
    directory where plow-init expects one, 60 seconds into boot. down and
    logs are unaffected regardless: an operator whose plow-init agent lost
    its credentials mid-migration must still be able to stop it or read its
    logs, the position they are in when something has already gone wrong.
    All seven routed through `agent-mgr compose <name> <verb>`, which reaches
    the same seam either through transition() or directly."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    if materialized:
        home.parent.mkdir(parents=True, exist_ok=True)
        (home.parent / ".plow-credentials-rowan").write_text(
            "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
        )
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("compose", "rowan", *argv, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert (r.returncode == 0) is allowed, r.stderr
    if not allowed:
        assert "run 'agent-mgr deploy rowan' first" in r.stderr
