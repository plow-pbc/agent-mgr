"""boot_contract.py: derived from the image's own baked HERMES_HOME, never a
separate descriptor field -- and the pieces that follow from that: pulling the
image before deriving anything, ensuring (not guarding) the current
contract's credential file, and refusing a resume that would skip it."""
import json
import os
from pathlib import Path

import pytest

from conftest import ROOT, fake_docker


def _stub_docker(tmp_path, script):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "docker").write_text(script)
    (b / "docker").chmod(0o755)
    return b


@pytest.mark.parametrize("home", ["/opt/data", "/var/lib/hermes"])
def test_home_target_recognises_both_contracts(tmp_path, monkeypatch, home):
    b = _stub_docker(tmp_path, f"#!/usr/bin/env bash\necho '[\"HERMES_HOME={home}\"]'\n")
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    from agent_mgr import boot_contract
    assert boot_contract.home_target("some/image:tag") == home


def test_home_target_fails_loudly_on_an_unrecognised_home(tmp_path, monkeypatch):
    b = _stub_docker(tmp_path, "#!/usr/bin/env bash\necho '[\"HERMES_HOME=/srv/other\"]'\n")
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    from agent_mgr import boot_contract
    from agent_mgr.errors import AgentMgrError
    with pytest.raises(AgentMgrError) as exc:
        boot_contract.home_target("some/image:tag")
    assert "does not recognise" in str(exc.value)
    assert "/srv/other" in str(exc.value)


def test_home_target_is_none_when_the_image_cannot_be_inspected(tmp_path, monkeypatch):
    """Not present locally, or no docker at all -- a diagnostic caller omits
    the field rather than guessing; never confused with an unrecognised
    HERMES_HOME on an image that DID answer."""
    b = _stub_docker(tmp_path, "#!/usr/bin/env bash\nexit 1\n")
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    from agent_mgr import boot_contract
    assert boot_contract.home_target("some/image:tag") is None


def test_ensure_image_local_pulls_only_when_absent(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    b = _stub_docker(tmp_path, (
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        'case "$*" in\n'
        '  *"image inspect"*) exit 1 ;;\n'
        '  *pull*) exit 0 ;;\n'
        'esac\n'
    ))
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    from agent_mgr import boot_contract
    boot_contract.ensure_image_local("some/image:tag")
    calls = log.read_text()
    assert "image inspect" in calls and "pull" in calls


def test_a_build_based_agents_contract_comes_from_the_fleet_pin_not_its_local_tag(
        run, instance, tmp_path):
    """AGENT_IMAGE for a build-based agent is a local tag Compose builds
    itself -- agent-mgr can neither pull nor inspect it before the first
    build, so the contract must come from the fleet's own pinned base
    instead, which a derived image always extends."""
    local_tag = "sams-str-hermes-agent:local"
    repo = instance("str", descriptor=f"AGENT_IMAGE={local_tag}\n")
    run("register", "str", str(repo))
    home = tmp_path / "home" / ".hermes-str"
    cfg = json.dumps({
        "name": "hermes-str",
        "services": {
            "hermes": {
                "container_name": "hermes-str",
                "environment": {"AGENT_ID": "str"},
                "build": {"context": "."},
                "image": local_tag,
                "pull_policy": "never",
                "volumes": [{"target": "/opt/data", "source": str(home)}],
            }
        },
    })
    # Placed in tmp_path/"bin" -- the SAME directory run()'s own fake curl/gh
    # live in -- so this replaces only docker, never dropping the fakes
    # install-plugin needs.
    _stub_docker(tmp_path, (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        # The local tag: not a registry reference -- never inspectable,
        # never pullable. Any call naming it must fail.
        f'  *"{local_tag}"*) exit 1 ;;\n'
        "  *\"Config.Env\"*) echo '[\"HERMES_HOME=/opt/data\"]' ;;\n"
        "  *\"image inspect\"*) exit 0 ;;\n"
        f"  *\"config --format json\"*) cat <<'JSON'\n{cfg}\nJSON\n    ;;\n"
        "  *\"ps -a --quiet\"*) : ;;\n"
        "  *\"ps --status running --quiet\"*) : ;;\n"
        "esac\n"
        "exit 0\n"
    ))
    r = run("deploy", "str")
    assert r.returncode == 0, r.stderr


def test_ensure_image_local_does_not_pull_when_present(tmp_path, monkeypatch):
    log = tmp_path / "calls.log"
    b = _stub_docker(tmp_path, f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {log}\nexit 0\n")
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    from agent_mgr import boot_contract
    boot_contract.ensure_image_local("some/image:tag")
    assert "pull" not in log.read_text()


def _resolved_agent(monkeypatch, run, instance, registry, tmp_path, name="rowan"):
    """A real ResolvedAgent, the way test_credentials.py builds one: through
    the CLI for setup, then resolved directly for the collaborator under test."""
    monkeypatch.syspath_prepend(str(ROOT))
    from agent_mgr.descriptor import resolve_agent
    from agent_mgr.registry import Registry
    run("register", name, str(instance(name)), check=True)
    run("deploy", name, check=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return resolve_agent(name, Registry(registry), ROOT)


def test_ensure_credentials_writes_from_the_dotenv(monkeypatch, run, instance, registry, tmp_path):
    from agent_mgr import boot_contract
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    (agent.home / ".env").write_text("PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n")
    destination = boot_contract.ensure_credentials(agent)
    assert destination == agent.home / "credentials.host"
    assert destination.read_text() == "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n"


def test_ensure_credentials_leaves_an_existing_file_alone(monkeypatch, run, instance, registry, tmp_path):
    """After first boot the current base truncates these keys out of the
    dotenv, so this file is the only remaining copy -- refreshing it from an
    empty dotenv would erase the agent's credential, not protect it."""
    from agent_mgr import boot_contract
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    destination = agent.home / "credentials.host"
    destination.write_text("PLOW_API_BASE=https://old.example\nPLOW_AGENT_TOKEN=tok_old\n")
    (agent.home / ".env").write_text("PLOW_HOME_CHANNEL=cht_x\nPLOW_AGENT_TOKEN=\n")
    result = boot_contract.ensure_credentials(agent)
    assert result == destination
    assert destination.read_text() == "PLOW_API_BASE=https://old.example\nPLOW_AGENT_TOKEN=tok_old\n"


def test_ensure_credentials_fails_loudly_when_neither_exists(monkeypatch, run, instance, registry, tmp_path):
    from agent_mgr import boot_contract
    from agent_mgr.errors import AgentMgrError
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    with pytest.raises(AgentMgrError) as exc:
        boot_contract.ensure_credentials(agent)
    assert "rowan" in str(exc.value) and "activate" in str(exc.value)


def test_require_running_contract_matches_refuses_on_a_stale_container(
        monkeypatch, run, instance, registry, tmp_path):
    """cron_sync and check_connectors run via exec, which Compose's own
    image-diff never protects -- a container created under one contract but
    now resolving to another must be caught directly."""
    from agent_mgr import boot_contract
    from agent_mgr.errors import AgentMgrError
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    b = tmp_path / "stale-bin"
    b.mkdir()
    (b / "docker").write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *stalecontainer*) echo '[\"HERMES_HOME=/var/lib/hermes\"]' ;;\n"
        "  *) echo '[\"HERMES_HOME=/opt/data\"]' ;;\n"
        "esac\n"
    )
    (b / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(AgentMgrError) as exc:
        boot_contract.require_running_contract_matches(agent, "stalecontainer")
    assert "rowan" in str(exc.value)
    assert "/var/lib/hermes" in str(exc.value) and "/opt/data" in str(exc.value)


def _seed_credentials(tmp_path, name):
    home = tmp_path / "home" / f".hermes-{name}"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n")
    return home


def test_up_force_recreates_for_the_current_contract_only(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    home = _seed_credentials(tmp_path, "rowan")
    log = tmp_path / "current.log"
    b = fake_docker(tmp_path, home=home, name="rowan", home_env="/var/lib/hermes",
                    running=False, log=log)
    r = run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert "--force-recreate" in log.read_text()
    assert (home / "credentials.host").read_text() == (
        "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n"
    )

    run("register", "legacy", str(instance("legacy")))
    home2 = tmp_path / "home" / ".hermes-legacy"
    log2 = tmp_path / "legacy.log"
    b2 = fake_docker(tmp_path, home=home2, name="legacy", running=False, log=log2)
    r2 = run("up", "legacy", env={"PATH": f"{b2}:{os.environ['PATH']}"})
    assert r2.returncode == 0, r2.stderr
    assert "--force-recreate" not in log2.read_text()


@pytest.mark.parametrize("verb", ["start", "restart", "unpause"])
@pytest.mark.parametrize(("home_env", "refused"), [("/var/lib/hermes", True), ("/opt/data", False)])
def test_compose_refuses_a_native_resume_for_the_current_contract_only(
        run, instance, tmp_path, verb, home_env, refused):
    """cont-init only runs at container creation, so a resumed container can
    never pick up a refreshed credential -- `up`/`restart` force-recreate
    instead."""
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    home_env=home_env)
    r = run("compose", "rowan", verb, env={"PATH": f"{b}:{os.environ['PATH']}"})
    if refused:
        assert r.returncode != 0
        assert "current-contract" in r.stderr
    else:
        assert r.returncode == 0, r.stderr
