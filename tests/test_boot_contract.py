"""AGENT_BOOT_CONTRACT: the descriptor key that selects compose.yml (today's
shape) or compose.plow-init.yml (see that file). No agent renders from the
new shape until its own descriptor sets this -- these tests are the proof
that setting it, and only setting it, is what moves the needle.
"""
import os

from conftest import ROOT, fake_docker

from agent_mgr.descriptor import resolve_agent
from agent_mgr.local import SCRUB
from agent_mgr.registry import Registry


def _agent(run, instance, registry, tmp_path, monkeypatch, name="rowan", descriptor=""):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    run("register", name, str(instance(name, descriptor=descriptor)), check=True)
    return resolve_agent(name, Registry(registry), ROOT)


def test_an_agent_without_the_key_keeps_todays_home_target(run, instance, registry, tmp_path, monkeypatch):
    agent = _agent(run, instance, registry, tmp_path, monkeypatch)
    assert agent.plow_init is False
    assert agent.home_mount_target == "/opt/data"
    assert agent.boot_contract == ""


def test_plow_init_selects_the_new_home_target_and_a_credentials_path_beside_the_home(
    run, instance, registry, tmp_path, monkeypatch
):
    agent = _agent(run, instance, registry, tmp_path, monkeypatch,
                   descriptor="AGENT_BOOT_CONTRACT=plow-init\n")
    assert agent.plow_init is True
    assert agent.home_mount_target == "/var/lib/hermes"
    assert agent.credentials == agent.home.parent / ".plow-credentials-rowan"
    assert not agent.credentials.is_relative_to(agent.home)


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


def test_resolve_guard_refuses_a_plow_init_agent_with_no_credentials_file(run, instance, tmp_path):
    """Compose does not check that a bind mount's source exists -- without this,
    `deploy`/`up` would bind-mount an empty directory at AGENT_CREDENTIALS and
    hand plow-init a directory instead of a file, 60 seconds into boot. Every
    transition (deploy, up, restart) goes through resolve_guard, so catching
    it here is catching it on every one of those, not just materialization."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("resolve-guard", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "materialize its credentials first" in r.stderr
    assert "rowan" in r.stderr


def test_resolve_guard_passes_for_a_plow_init_agent_once_credentials_exist(run, instance, tmp_path):
    """Closes the loop past the renderer: agent-mgr's own guard must read the
    new home target it just taught compose.plow-init.yml to mount at."""
    home = tmp_path / "home" / ".hermes-rowan"
    run("register", "rowan",
        str(instance("rowan", descriptor="AGENT_BOOT_CONTRACT=plow-init\n")), check=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".plow-credentials-rowan").write_text(
        "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_secret\n"
    )
    b = fake_docker(tmp_path, home=home, name="rowan", target="/var/lib/hermes")
    r = run("resolve-guard", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr + r.stdout
