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
        (("run", "--entrypoint", "/bin/true", "hermes"), False, False),
        (("down",), False, True),
        (("logs",), False, True),
    ],
    ids=["up-refused", "up-allowed", "create-refused", "run-refused",
         "down-allowed", "logs-allowed"],
)
def test_the_credential_gate_covers_every_start_verb_and_only_those(
    run, instance, tmp_path, argv, materialized, allowed
):
    """One seam (compose() in local.py) gates every verb that creates or
    (re)starts a container -- up, create, and run (which bypasses
    transition() entirely via LEAVES_RUNNING) -- against a missing
    credentials file, the failure that would otherwise bind-mount an empty
    directory where plow-init expects one, 60 seconds into boot. down and
    logs are unaffected regardless: an operator whose plow-init agent lost
    its credentials mid-migration must still be able to stop it or read its
    logs, the position they are in when something has already gone wrong.
    (start/restart are covered separately: transition() refuses them
    outright for a plow-init agent, credentials or not -- see below.)"""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    if materialized:
        home.mkdir(parents=True, exist_ok=True)
        credential_text = "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
        (home / ".env").write_text(credential_text)
        (home.parent / ".plow-credentials-rowan").write_text(credential_text)
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("compose", "rowan", *argv, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert (r.returncode == 0) is allowed, r.stderr
    if not allowed:
        assert "run 'agent-mgr deploy rowan' first" in r.stderr


def _plow_init_agent_with_matching_credentials(run, instance, tmp_path, name="rowan"):
    """A plow-init agent whose materialized credentials already agree with
    its current home dotenv -- the normal, post-deploy state."""
    home = tmp_path / "home" / f".hermes-{name}"
    run("register", name,
        str(instance(name, descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    home.mkdir(parents=True)
    credential_text = "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
    (home / ".env").write_text(credential_text)
    (home.parent / f".plow-credentials-{name}").write_text(credential_text)
    return home


@pytest.mark.parametrize("argv", [("start",), ("restart",)], ids=["start", "restart"])
def test_native_start_and_restart_are_refused_for_a_plow_init_agent_even_with_valid_credentials(
    run, instance, tmp_path, argv
):
    """Neither recreates the container by nature -- an already-created
    container can stay bound to a credential a rotation has since replaced
    on disk, even though the file itself is perfectly valid right now.
    Refused outright rather than trusted, with the safe verb named."""
    home = _plow_init_agent_with_matching_credentials(run, instance, tmp_path)
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("compose", "rowan", *argv, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "run 'agent-mgr up rowan' instead" in r.stderr


def test_up_always_force_recreates_for_a_plow_init_agent(run, instance, tmp_path):
    """The other half of the guarantee: up is the one verb that recreates,
    so it -- not start/restart -- is the safe way to (re)start a plow-init
    agent after a credential rotation, whether or not one just happened."""
    home = _plow_init_agent_with_matching_credentials(run, instance, tmp_path)
    log = tmp_path / "docker.log"
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes", log=log)
    r = run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert "--force-recreate" in log.read_text()


def test_a_repointed_home_is_refused_not_silently_booted_with_the_old_credential(
    run, instance, tmp_path
):
    """credentials is keyed by agent NAME, not by home, so repointing a
    registered name at a different AGENT_HOME leaves the previous home's
    materialized file in place. The path existing is not proof it belongs
    to the CURRENT home -- only comparing values catches this."""
    old_home = tmp_path / "home" / ".hermes-rowan-old"
    old_home.mkdir(parents=True)
    old_credentials = "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_old\n"
    (old_home / ".env").write_text(old_credentials)
    (tmp_path / "home" / ".plow-credentials-rowan").write_text(old_credentials)

    new_home = tmp_path / "home" / "rowan-new-repo" / ".hermes"
    new_home.mkdir(parents=True)
    (new_home / ".env").write_text(
        "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_new\n"
    )
    run("register", "rowan",
        str(instance("rowan",
                     descriptor=f"AGENT_HOME={new_home}\nAGENT_BOOT_CONTRACT=plow-init\n")),
        check=True)

    # fake_docker's own default credential-mount source assumes home.parent
    # IS the operator's home, which does not hold for this non-standard
    # layout -- tell it the real one, so resolve_guard's own mount check
    # (proven correct in test_resolve_guard.py) does not fire here instead.
    b = fake_docker(tmp_path, home=new_home, name="rowan", target="/var/lib/hermes",
                    credentials=tmp_path / "home" / ".plow-credentials-rowan")
    r = run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})

    assert r.returncode != 0
    assert "do not match its current home dotenv" in r.stderr
