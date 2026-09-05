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


def test_a_build_based_agents_own_image_determines_its_contract_even_when_current(
        run, instance, tmp_path):
    """An already-built local tag is inspected DIRECTLY -- never substituted
    for the fleet's pinned base, which would silently pick the legacy target
    for an image that has already moved to the current contract. This is not
    hypothetical: an operator's own build-based agent can already be current
    while the fleet-wide pin is still legacy."""
    stock = json.loads((ROOT / "runtime" / "stack.json").read_text())["images"]["hermes_local"][
        "reference"
    ]
    local_tag = "sams-str-hermes-agent:local"
    repo = instance("str", descriptor=f"AGENT_IMAGE={local_tag}\n")
    run("register", "str", str(repo))
    _stub_docker(tmp_path, (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        # The fleet's own pinned base is still legacy. Substituting IT for
        # the already-built local tag below would be exactly the bug under
        # test -- resolve would report legacy for an agent that is current.
        f'  *"Config.Env"*"{stock}"*) echo \'["HERMES_HOME=/opt/data"]\' ;;\n'
        f'  *"Config.Env"*"{local_tag}"*) echo \'["HERMES_HOME=/var/lib/hermes"]\' ;;\n'
        "  *\"image inspect\"*) exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    ))
    r = run("resolve", "str")
    assert r.returncode == 0, r.stderr
    assert "AGENT_HOME_TARGET=/var/lib/hermes" in r.stdout


def test_deploy_builds_rather_than_pulls_a_not_yet_present_local_tag(run, instance, tmp_path):
    """A build-based agent's own tag is not a registry reference at all --
    deploy must build it (from its own override alone), never attempt to
    pull it, and the built image is what gets inspected afterward."""
    local_tag = "sams-str-hermes-agent:local"
    repo = instance("str", descriptor=f"AGENT_IMAGE={local_tag}\n")
    (repo / "compose.override.yml").write_text(
        "services:\n  hermes:\n    build: { context: . }\n"
        f"    image: {local_tag}\n    pull_policy: never\n"
    )
    run("register", "str", str(repo))
    home = tmp_path / "home" / ".hermes-str"
    built_marker = tmp_path / "built"
    log = tmp_path / "calls.log"
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
        f'printf "%s\\n" "$*" >> {log}\n'
        "case \"$*\" in\n"
        # Anchored at the end -- a bare substring match on "build" would also
        # catch this TEST's own tmp_path, which pytest names after the test
        # function and which appears in every invocation's --env-file path.
        f'  *" build")  touch {built_marker} ;;\n'
        # Absent until the build "runs" above -- present after, the way a
        # real image would be.
        f'  *"Config.Env"*) [ -e {built_marker} ] && echo \'["HERMES_HOME=/opt/data"]\' || exit 1 ;;\n'
        f'  *"image inspect"*) [ -e {built_marker} ] && exit 0 || exit 1 ;;\n'
        f"  *\"config --format json\"*) cat <<'JSON'\n{cfg}\nJSON\n    ;;\n"
        "  *\"ps -a --quiet\"*) : ;;\n"
        "  *\"ps --status running --quiet\"*) : ;;\n"
        "esac\n"
        "exit 0\n"
    ))
    r = run("deploy", "str")
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "build" in calls
    assert "pull" not in calls


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


def test_credentials_host_path_lives_outside_every_agent_home(
        monkeypatch, run, instance, registry, tmp_path):
    """Not a symlink escape hatch: the path is computed independently of
    whatever the agent's own (possibly compromised) container has put inside
    its home, including a symlink named exactly like an in-home path would
    have been."""
    from agent_mgr import boot_contract
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    path = boot_contract.credentials_host_path(agent)
    assert path == Path.home() / f".plow-credentials-{agent.name}"
    assert agent.home not in path.parents


def test_ensure_credentials_writes_from_the_dotenv(monkeypatch, run, instance, registry, tmp_path):
    from agent_mgr import boot_contract
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    (agent.home / ".env").write_text("PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n")
    destination = boot_contract.ensure_credentials(agent)
    expected = boot_contract.credentials_host_path(agent)
    assert destination == expected
    assert not str(expected).startswith(str(agent.home)), "must live outside every agent's home"
    assert destination.read_text() == "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n"


def test_ensure_credentials_leaves_an_existing_file_alone(monkeypatch, run, instance, registry, tmp_path):
    """After first boot the current base truncates these keys out of the
    dotenv, so this file is the only remaining copy -- refreshing it from an
    empty dotenv would erase the agent's credential, not protect it."""
    from agent_mgr import boot_contract
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    destination = boot_contract.credentials_host_path(agent)
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


def _seed_credentials(tmp_path, name):
    home = tmp_path / "home" / f".hermes-{name}"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n")
    return home


@pytest.mark.parametrize(("name", "home_env", "container_home_env"), [
    ("current", "/var/lib/hermes", None),
    ("legacy", "/opt/data", None),
    # The migration itself: a RUNNING container still baked (and mounted)
    # legacy while its replacement image is already current. Ownership keys
    # off the CONTAINER's own home, so this must not read as foreign -- and
    # the current image still forces the recreate cont-init needs.
    ("migrating", "/var/lib/hermes", "/opt/data"),
])
def test_up_force_recreates_for_the_current_contract_only(
        run, instance, tmp_path, name, home_env, container_home_env):
    run("register", name, str(instance(name)))
    home = _seed_credentials(tmp_path, name)
    log = tmp_path / f"{name}.log"
    b = fake_docker(tmp_path, home=home, name=name, home_env=home_env,
                    container_home_env=container_home_env,
                    running=container_home_env is not None, log=log)
    r = run("up", name, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    current = home_env == "/var/lib/hermes"
    assert ("--force-recreate" in log.read_text()) == current
    # Outside home -- the run() fixture points HOME at tmp_path/"home" for
    # the subprocess, which is what credentials_host_path() resolves against.
    credentials_host = tmp_path / "home" / f".plow-credentials-{name}"
    assert not str(credentials_host).startswith(str(home))
    written = credentials_host.read_text() if credentials_host.is_file() else ""
    assert written == (
        "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n" if current else ""
    )


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


@pytest.mark.parametrize("run_args", [
    ("down", "rowan"),
    ("compose", "rowan", "stop"),
    ("compose", "rowan", "kill"),
    ("compose", "rowan", "pause"),
])
def test_ensure_credentials_does_not_run_for_shutdown_verbs(run, instance, tmp_path, run_args):
    """down/stop/kill/pause create no container, so writing (or requiring) a
    credential for them would be pointless at best and a spurious refusal at
    worst -- no PLOW_API_BASE/PLOW_AGENT_TOKEN exist anywhere here, which
    would make ensure_credentials refuse loudly if it ran at all."""
    run("register", "rowan", str(instance("rowan")))
    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True)
    b = fake_docker(tmp_path, home=home, name="rowan", home_env="/var/lib/hermes")
    r = run(*run_args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr


def test_a_legacy_container_is_not_foreign_after_a_pin_bump_to_current(run, instance, tmp_path):
    """During a migration the running container's own baked contract can
    differ from what the agent NOW resolves to -- the ownership check must
    key off the CONTAINER's own HERMES_HOME to find its mount, not the
    replacement image's target, or a perfectly valid old mount reads as
    foreign and blocks deploy/restart/up, including the recovery the refusal
    itself would suggest."""
    repo = instance("rowan")
    run("register", "rowan", str(repo))
    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True)
    (home / ".env").write_text("PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n")
    cfg = json.dumps({
        "name": "hermes-rowan",
        "services": {
            "hermes": {
                "container_name": "hermes-rowan",
                "environment": {"AGENT_ID": "rowan"},
                "image": "nousresearch/hermes-agent@sha256:" + "c" * 64,
                # What the REPLACEMENT (current-contract) config resolves to.
                "volumes": [{"target": "/var/lib/hermes", "source": str(home)}],
            }
        },
    })
    _stub_docker(tmp_path, (
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        # The agent's image now resolves current (a pin bump)...
        "  *\"Config.Env\"*deadbeef*) echo '[\"HERMES_HOME=/opt/data\"]' ;;\n"
        "  *\"Config.Env\"*) echo '[\"HERMES_HOME=/var/lib/hermes\"]' ;;\n"
        # ...but the EXISTING container (deadbeef) was created under the OLD,
        # legacy contract, and still genuinely mounts the home there -- a
        # destination filter for anything else must find nothing, the way a
        # real daemon would.
        f'  *Mounts*\'"/opt/data"\'*) echo {home} ;;\n'
        "  *Mounts*) : ;;\n"
        "  *\"image inspect\"*) exit 0 ;;\n"
        f"  *\"config --format json\"*) cat <<'JSON'\n{cfg}\nJSON\n    ;;\n"
        "  *\"ps -a --quiet\"*) echo deadbeef ;;\n"
        "  *\"ps --status running --quiet\"*) echo deadbeef ;;\n"
        "esac\n"
        "exit 0\n"
    ))
    r = run("restart", "rowan")
    assert r.returncode == 0, r.stderr
