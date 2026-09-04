"""AGENT_BOOT_CONTRACT: the descriptor key that selects compose.yml (today's
shape) or compose.plow-init.yml (see that file). No agent renders from the
new shape until its own descriptor sets this -- these tests are the proof
that setting it, and only setting it, is what moves the needle.
"""
import os

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
    assert agent.credentials == agent.home.parent / ".plow-credentials-rowan"
    assert not agent.credentials.is_relative_to(agent.home)
    # An agent repo's own compose.override.yml mounts a skill or SOUL.md
    # against this, not a hardcoded path, so it survives a contract change.
    assert agent.environment()["AGENT_HOME_TARGET"] == target


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


def test_up_refuses_a_plow_init_agent_with_no_credentials_file(run, instance, tmp_path):
    """Compose does not check that a bind mount's source exists -- without
    this, `up` would bind-mount an empty directory at AGENT_CREDENTIALS and
    hand plow-init a directory instead of a file, 60 seconds into boot. The
    refusal names the fix: 'agent-mgr deploy' now materializes credentials
    for a plow-init agent, so it is a real, runnable remediation."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "run 'agent-mgr deploy rowan' first" in r.stderr


def test_up_passes_for_a_plow_init_agent_once_credentials_exist(run, instance, tmp_path):
    """Closes the loop past the renderer: starting the container must read
    the new home target it just taught compose.plow-init.yml to mount at."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".plow-credentials-rowan").write_text(
        "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
    )
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr + r.stdout


@pytest.mark.parametrize("operation", ["down", "logs"])
def test_down_and_logs_stay_open_for_a_plow_init_agent_with_no_credentials_file(
    run, instance, tmp_path, operation
):
    """The credential check gates STARTING a container, not every transition:
    an operator whose plow-init agent lost its credentials mid-migration must
    still be able to stop it or read its logs -- exactly the position they
    are in when something has already gone wrong."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run(operation, "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr + r.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ("create",),
        ("start",),
        ("restart",),
        ("run", "--entrypoint", "/bin/true", "hermes"),
    ],
    ids=["create", "start", "restart", "run"],
)
def test_every_compose_passthrough_start_verb_refuses_a_plow_init_agent_with_no_credentials(
    run, instance, tmp_path, argv
):
    """`agent-mgr up` was covered directly; this is the `agent-mgr compose
    <name> <verb>` passthrough -- native `create`/`start`/`restart` reach
    transition() but are not the literal string "up" (`create` materializes
    the bind mount without even starting the container), and `run` bypasses
    transition() entirely via LEAVES_RUNNING. All four still resolve the
    same credentials bind-mount `up` does."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("compose", "rowan", *argv, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "run 'agent-mgr deploy rowan' first" in r.stderr
