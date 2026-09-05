"""boot_contract.py: derived from the image's own baked HERMES_HOME, never a
separate descriptor field -- and the pieces that follow from that: pulling the
image before deriving anything, ensuring (not guarding) the current
contract's credential file, and refusing a resume that would skip it."""
import json
import os

import pytest

from conftest import ROOT, fake_docker


def _stub_docker(tmp_path, script):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "docker").write_text(script)
    (b / "docker").chmod(0o755)
    return b


LOCAL_TAG = "sams-str-hermes-agent:local"


def _build_based_agent(run, instance):
    """A registered agent whose image is its OWN build rather than a registry
    pin -- the only shape either build path exists for."""
    repo = instance("str", descriptor=f"AGENT_IMAGE={LOCAL_TAG}\n")
    (repo / "compose.override.yml").write_text(
        "services:\n  hermes:\n    build: { context: . }\n"
        f"    image: {LOCAL_TAG}\n    pull_policy: never\n"
    )
    run("register", "str", str(repo))
    return repo


@pytest.mark.parametrize(("env", "baked"), [
    (["HERMES_HOME=/opt/data"], "/opt/data"),
    (["HERMES_HOME=/var/lib/hermes"], "/var/lib/hermes"),
    # A home agent-mgr cannot boot is loud, never guessed...
    (["HERMES_HOME=/srv/other"], "/srv/other"),
    # ...including one that bakes no home at all, where docker DID answer:
    # "not present locally -- run deploy" would send the operator to a deploy
    # that finds the image already local and refuses all over again.
    (["PATH=/usr/bin"], ""),
    # ...and never confused with docker having no answer at all -- absent
    # locally, or not on PATH -- where a diagnostic caller omits the field.
    (None, None),
])
def test_home_target_maps_docker_inspection(tmp_path, monkeypatch, env, baked):
    body = "exit 1" if env is None else f"echo '{json.dumps(env)}'"
    b = _stub_docker(tmp_path, f"#!/usr/bin/env bash\n{body}\n")
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    from agent_mgr import boot_contract
    from agent_mgr.errors import AgentMgrError
    if baked is None or baked in boot_contract.KNOWN_HOME_TARGETS:
        assert boot_contract.home_target("some/image:tag") == baked
        return
    with pytest.raises(AgentMgrError) as exc:
        boot_contract.home_target("some/image:tag")
    assert "does not recognise" in str(exc.value) and repr(baked) in str(exc.value)


@pytest.mark.parametrize(("inspect_status", "pulled"), [(1, True), (0, False)])
def test_ensure_image_local_pulls_iff_absent(tmp_path, monkeypatch, inspect_status, pulled):
    log = tmp_path / "calls.log"
    b = _stub_docker(tmp_path, (
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        'case "$*" in\n'
        f'  *"image inspect"*) exit {inspect_status} ;;\n'
        '  *pull*) exit 0 ;;\n'
        'esac\n'
    ))
    monkeypatch.setenv("PATH", f"{b}{os.pathsep}{os.environ['PATH']}")
    from agent_mgr import boot_contract
    boot_contract.ensure_image_local("some/image:tag")
    calls = log.read_text()
    assert "image inspect" in calls
    assert ("pull" in calls) is pulled


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
    repo = _build_based_agent(run, instance)
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
                "image": LOCAL_TAG,
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


def test_the_documented_build_escape_runs_before_the_contract_it_creates(run, instance, tmp_path):
    """Every --build/--pull refusal names `compose <name> build` as the step to
    run first, so it cannot itself demand the boot contract of the image it
    exists to create. It takes deploy's own absent-image route -- the override
    alone, never the contract-bearing template stack -- and carries the
    operator's trailing build flags there."""
    override = _build_based_agent(run, instance) / "compose.override.yml"
    log = tmp_path / "calls.log"
    # The image is genuinely absent -- no Config.Env to read, which is exactly
    # the state resolve_guard turned into "run deploy first".
    _stub_docker(tmp_path, (
        "#!/usr/bin/env bash\n"
        f'printf "%s AGENT_HOME_TARGET=%s\\n" "$*" "${{AGENT_HOME_TARGET:-}}" >> {log}\n'
        'case "$*" in\n'
        '  *"Config.Env"*|*"image inspect"*) exit 1 ;;\n'
        "esac\n"
        "exit 0\n"
    ))
    r = run("compose", "str", "build", "--no-cache")
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "build --no-cache" in calls, "the operator's own build flags were dropped"
    assert "templates/compose.yml" not in calls, "the build went back through the contract stack"
    # Compose interpolates the override before it knows the verb, so a
    # `${AGENT_HOME_TARGET:?}` volume line refuses a build unless the variable
    # is set -- and a build can set nothing true, only something a mount could
    # never satisfy.
    build_line = next(line for line in calls.splitlines() if "build --no-cache" in line)
    assert "AGENT_HOME_TARGET=/agent-mgr-build-has-no-contract" in build_line

    # And an agent with no override of its own has nothing to build -- loudly,
    # since the one path now serves an operator typing this directly.
    override.unlink()
    r = run("compose", "str", "build")
    assert r.returncode != 0
    assert "declares no image of its own" in r.stderr


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


FRESH = "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_x\n"
KEPT = "PLOW_API_BASE=https://old.example\nPLOW_AGENT_TOKEN=tok_old\n"


@pytest.mark.parametrize(("dotenv", "existing", "expected"), [
    pytest.param(FRESH, None, FRESH, id="dotenv-wins"),
    # After first boot the current base truncates these keys out of the
    # dotenv, so this file is the only remaining copy -- refreshing it from an
    # empty dotenv would erase the agent's credential, not protect it.
    pytest.param("PLOW_HOME_CHANNEL=cht_x\nPLOW_AGENT_TOKEN=\n", KEPT, KEPT, id="existing-file"),
    pytest.param(None, None, None, id="missing"),
])
def test_ensure_credentials_resolves_the_only_host_side_copy(
        monkeypatch, run, instance, registry, tmp_path, dotenv, existing, expected):
    from agent_mgr import boot_contract
    from agent_mgr.errors import AgentMgrError
    agent = _resolved_agent(monkeypatch, run, instance, registry, tmp_path)
    destination = boot_contract.credentials_host_path(agent)
    if dotenv:
        (agent.home / ".env").write_text(dotenv)
    if existing:
        destination.write_text(existing)
    if expected is None:
        with pytest.raises(AgentMgrError) as exc:
            boot_contract.ensure_credentials(agent)
        assert "rowan" in str(exc.value) and "activate" in str(exc.value)
        return
    assert boot_contract.ensure_credentials(agent) == destination
    assert destination.read_text() == expected


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


@pytest.mark.parametrize(("run_args", "staged"), [
    # Every verb that makes a container, through both dispatch paths -- the
    # image's cont-init promotes the credential at creation and never again,
    # and its read-only bind source must already BE a file or Docker leaves a
    # directory there that os.replace() can never replace.
    (("up", "rowan"), True),
    (("compose", "rowan", "create"), True),
    (("compose", "rowan", "run", "--entrypoint", "true"), True),
    # And the verbs that make none, where writing (or requiring) a credential
    # would be pointless at best and a spurious refusal at worst.
    (("down", "rowan"), False),
    (("compose", "rowan", "stop"), False),
    (("compose", "rowan", "kill"), False),
    (("compose", "rowan", "pause"), False),
])
def test_only_container_creating_verbs_stage_the_current_credential(
        run, instance, tmp_path, run_args, staged):
    run("register", "rowan", str(instance("rowan")))
    home = _seed_credentials(tmp_path, "rowan")
    b = fake_docker(tmp_path, home=home, name="rowan", home_env="/var/lib/hermes")
    r = run(*run_args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "home" / ".plow-credentials-rowan").is_file() == staged
