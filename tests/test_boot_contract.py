"""AGENT_BOOT_CONTRACT: the per-home dotenv key that selects compose.yml
(today's shape) or compose.plow-init.yml (see that file). Read only from
AGENT_HOME/.env, never from the repo's agent.env, so one instance opting in
cannot flip a sibling sharing the same repo -- these tests are the proof
that setting it, and only setting it, on THIS instance's own home is what
moves the needle.
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
    "home_env, plow_init, target",
    [
        ("", False, "/opt/data"),
        ("AGENT_BOOT_CONTRACT=plow-init\n", True, "/var/lib/hermes"),
        ("AGENT_BOOT_CONTRACT=\n", False, "/opt/data"),
    ],
    ids=["absent", "plow-init", "blank"],
)
def test_the_boot_contract_selects_the_home_target_and_credentials_path(
    run, instance, registry, tmp_path, monkeypatch, home_env, plow_init, target
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run("register", "rowan", str(instance("rowan")), check=True)
    if home_env:
        home = tmp_path / "home" / ".hermes-rowan"
        home.mkdir(parents=True)
        (home / ".env").write_text(home_env)
    agent = resolve_agent("rowan", Registry(registry), ROOT)
    assert agent.plow_init is plow_init
    assert agent.home_mount_target == target
    assert agent.boot_contract == ("plow-init" if plow_init else "")
    assert agent.credentials == Path.home() / ".plow-credentials-rowan"
    assert not agent.credentials.is_relative_to(agent.home)
    # An agent repo's own compose.override.yml mounts a skill or SOUL.md
    # against this, not a hardcoded path, so it survives a contract change.
    assert agent.environment()["AGENT_HOME_TARGET"] == target


def test_a_sibling_sharing_the_repo_does_not_inherit_a_boot_contract_set_on_another_homes_dotenv(
    run, instance, registry, tmp_path, monkeypatch
):
    """Two names against the same repo (README's 'One repo, several people')
    resolve to separate homes -- rowan opting into plow-init in its OWN home
    dotenv must not flip bob, who shares the repo but has never opted in
    himself."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = instance("rowan")
    run("register", "rowan", str(repo), check=True)
    run("register", "bob", str(repo), check=True)
    rowan_home = tmp_path / "home" / ".hermes-rowan"
    rowan_home.mkdir(parents=True)
    (rowan_home / ".env").write_text("AGENT_BOOT_CONTRACT=plow-init\n")

    rowan = resolve_agent("rowan", Registry(registry), ROOT)
    bob = resolve_agent("bob", Registry(registry), ROOT)

    assert rowan.plow_init is True
    assert bob.plow_init is False
    assert bob.home_mount_target == "/opt/data"


def test_a_repo_level_boot_contract_does_not_opt_anyone_in(
    run, instance, registry, tmp_path, monkeypatch
):
    """AGENT_BOOT_CONTRACT in agent.env used to be the shared source of
    truth; it no longer is. Setting it there is a no-op, not a refusal --
    the repo descriptor simply does not own this key any more."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    agent = resolve_agent("rowan", Registry(registry), ROOT)
    assert agent.plow_init is False
    assert agent.home_mount_target == "/opt/data"


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
    repo_hermes.mkdir(parents=True)
    (repo_hermes / ".env").write_text("AGENT_BOOT_CONTRACT=plow-init\n")
    repo = instance("rowan", descriptor=f"AGENT_HOME={repo_hermes}\n")
    run("register", "rowan", str(repo), check=True)
    agent = resolve_agent("rowan", Registry(registry), ROOT)
    assert agent.home == repo_hermes
    assert agent.credentials == Path.home() / ".plow-credentials-rowan"
    assert not agent.credentials.is_relative_to(repo)


def test_a_typo_in_the_boot_contract_is_refused_not_silently_kept_on_todays_shape(
    run, instance, tmp_path
):
    """A typo here is the worst kind of bug: the agent looks opted in to
    whoever reads its home dotenv, but silently keeps running today's shape."""
    run("register", "rowan", str(instance("rowan")))
    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True)
    (home / ".env").write_text("AGENT_BOOT_CONTRACT=Plow-Init\n")
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
    (start/restart/unpause are covered separately: transition() refuses
    them outright for a plow-init agent, credentials or not -- see below.)"""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan", str(instance("rowan")), check=True)
    home.mkdir(parents=True, exist_ok=True)
    dotenv_text = "AGENT_BOOT_CONTRACT=plow-init\n"
    if materialized:
        credential_text = "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
        dotenv_text += credential_text
        (home.parent / ".plow-credentials-rowan").write_text(credential_text)
    (home / ".env").write_text(dotenv_text)
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("compose", "rowan", *argv, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert (r.returncode == 0) is allowed, r.stderr
    if not allowed:
        assert "run 'agent-mgr deploy rowan' first" in r.stderr


@pytest.mark.parametrize(
    "cli_args, refused",
    [
        (("compose", "rowan", "start"), True),
        (("compose", "rowan", "restart"), True),
        (("compose", "rowan", "unpause"), True),
        (("up", "rowan"), False),
    ],
    ids=["start-refused", "restart-refused", "unpause-refused", "up-force-recreates"],
)
def test_plow_init_transition_policy(run, instance, tmp_path, cli_args, refused):
    """transition() refuses every native verb that can resume an existing
    plow-init container without recreating it -- start, restart, unpause --
    since none of them re-resolves the bind mount, so none can be trusted to
    see a credential rotation already on disk, even with a currently-valid
    file. up is the one verb that is always safe, because this forces it to
    --force-recreate regardless of whether a rotation just happened."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan", str(instance("rowan")), check=True)
    home.mkdir(parents=True)
    credential_text = "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
    (home / ".env").write_text("AGENT_BOOT_CONTRACT=plow-init\n" + credential_text)
    (home.parent / ".plow-credentials-rowan").write_text(credential_text)
    log = tmp_path / "docker.log"
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes", log=log)
    r = run(*cli_args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    if refused:
        assert r.returncode != 0
        assert "run 'agent-mgr up rowan' instead" in r.stderr
    else:
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
        "AGENT_BOOT_CONTRACT=plow-init\nPLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_new\n"
    )
    run("register", "rowan",
        str(instance("rowan", descriptor=f"AGENT_HOME={new_home}\n")),
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
