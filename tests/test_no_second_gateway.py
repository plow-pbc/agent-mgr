"""The regression this whole tool exists to prevent.

The Hermes image's s6 entrypoint starts a gateway whatever command you pass it,
so `docker compose run ... chat -q` brings up a SECOND gateway against the same
/opt/data. It evicts the live one from its chat websockets and, on exit, posts a
shutdown notice into the owners' channel. Measured on one host over two days:
25 gateway starts against a 1-6/day baseline, 21 shutdown notices in a single
day, and 6 sqlite errors from two gateways racing one session database.
"""
import os
import re
from pathlib import Path

import pytest

from conftest import fake_docker
from test_install import _guarded

ROOT = Path(__file__).resolve().parent.parent
SOURCES = [ROOT / "agent-mgr", *sorted((ROOT / "lib").glob("*"))]

# A single- or double-quoted span. Stripped before matching, because a real
# invocation's `run` is a bare argument while the same word inside an error
# message or a `case` glob is not one.
QUOTED = re.compile(r"'[^']*'" + r'|"[^"]*"')
INVOCATION = re.compile(r"\bcompose\b[^#]*\brun\b")


def _invokes_compose_run(line):
    if line.lstrip().startswith("#"):
        return False
    return bool(INVOCATION.search(QUOTED.sub("", line)))


def test_no_source_file_invokes_compose_run():
    offenders = []
    for p in SOURCES:
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if _invokes_compose_run(line):
                offenders.append(f"{p.name}:{i}: {line.strip()}")
    assert not offenders, "compose run starts a rival gateway; use exec:\n" + "\n".join(offenders)


def test_that_check_is_not_vacuous():
    """Stripping quotes must not have blinded the guard to a real invocation."""
    assert _invokes_compose_run('        compose run --rm hermes chat -q "$*"')
    assert _invokes_compose_run('    docker compose --env-file x run --rm hermes')
    assert not _invokes_compose_run(
        """        *) die "refusing 'compose run' without --entrypoint" ;;""")
    assert not _invokes_compose_run('        # docker compose run would start a rival')


def _fake_docker(tmp_path, home, container="hermes-rowan", running=True):
    """conftest's builder plus an argv log, rather than a second 22-line copy.

    The log is what makes these assertions about what actually ran instead of
    what the source says -- a grep cannot tell an exec that runs from one
    sitting behind a condition that is false exactly when it matters.
    """
    log = tmp_path / "docker-argv.log"
    b = fake_docker(tmp_path, home=home, container=container, name="rowan",
                    running=running, log=log)
    return b, log


def test_the_agent_subcommand_runs_a_turn_through_exec(run, instance, tmp_path):
    """Asserted by running the real command against a docker that records argv --
    not by grepping the source, which cannot tell an exec that runs from one
    sitting behind a condition that is false exactly when it matters."""
    run("register", "rowan", str(instance("rowan")))
    home = tmp_path / "home" / ".hermes-rowan"
    b, log = _fake_docker(tmp_path, home)
    r = run("agent", "rowan", "what is on today?",
            env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "exec" in calls
    assert "chat -q" in calls
    assert "what is on today?" in calls
    assert re.search(r"\bcompose\b.*\brun\b", calls) is None, "a rival gateway was started"


def test_the_turn_runs_as_the_host_user_not_as_root(run, instance, tmp_path):
    """exec skips the entrypoint that remaps the in-image hermes user, so a plain
    exec runs as root and leaves root-owned files the gateway cannot rewrite."""
    run("register", "rowan", str(instance("rowan")))
    b, log = _fake_docker(tmp_path, tmp_path / "home" / ".hermes-rowan")
    run("agent", "rowan", "hello", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert f"--user {os.getuid()}:{os.getgid()}" in log.read_text()


def test_agent_refuses_when_no_gateway_is_running(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    b, _ = _fake_docker(tmp_path, tmp_path / "home" / ".hermes-rowan", running=False)
    r = run("agent", "rowan", "hello", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "not running" in r.stderr


def test_agent_refuses_when_the_resolved_container_is_not_this_agents(run, instance, tmp_path):
    """The guard runs before the turn, so a descriptor that resolves to someone
    else's container never reaches exec."""
    run("register", "rowan", str(instance("rowan")))
    b, log = _fake_docker(tmp_path, tmp_path / "home" / ".hermes-rowan", container="hermes")
    r = run("agent", "rowan", "hello", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "refusing to act" in r.stderr
    assert "chat -q" not in log.read_text()


def test_agent_with_no_prompt_is_refused(run, instance):
    run("register", "rowan", str(instance("rowan")))
    r = run("agent", "rowan")
    assert r.returncode != 0
    assert "usage" in r.stderr


@pytest.mark.parametrize("args, ok, expect", [
    # The passthrough exists for an agent's own domain recipes, so it must not
    # become the hole a rival gateway is started through -- but it must still
    # allow the shape those recipes actually use. Two live callers in the
    # rentals agent pass --entrypoint for exactly this reason.
    (["run", "--rm", "hermes", "chat", "-q", "hi"], False, "second gateway"),
    (["run", "--rm", "--entrypoint", "bash", "hermes", "-c", "true"], True, "--entrypoint bash"),
    (["ps"], True, "ps"),
])
def test_the_compose_passthrough_allows_only_what_starts_no_gateway(
        run, instance, tmp_path, args, ok, expect):
    run("register", "rowan", str(instance("rowan")))
    b, log = _fake_docker(tmp_path, tmp_path / "home" / ".hermes-rowan")
    r = run("compose", "rowan", *args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    if ok:
        assert r.returncode == 0, r.stderr
        assert expect in log.read_text()
    else:
        assert r.returncode != 0
        assert expect in r.stderr
        assert not log.exists() or "chat" not in log.read_text()


def test_the_compose_passthrough_still_runs_the_guard(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    b, _ = _fake_docker(tmp_path, tmp_path / "home" / ".hermes-rowan", container="hermes")
    r = run("compose", "rowan", "ps", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


@pytest.mark.parametrize("args, refused, why", [
    (("scale", "hermes=0"), True,
     "scale stops a container and was in neither list when the guard enumerated "
     "stoppers -- naming what is SAFE means an unknown subcommand asks the veto"),
    (("wait", "hermes", "--down-project"), True,
     "wait --down-project drops the whole project: membership of the safe list "
     "has to hold under every flag the subcommand accepts"),
    (("--project-name", "logs", "down"), True,
     "scanning the argv for the first recognised word let a global option's "
     "VALUE stand in for the subcommand -- it is $1 now"),
    (("run", "--rm", "--entrypoint", "bash", "hermes"), False,
     "a throwaway container beside the live one stops nothing, and refusing it "
     "would break the maintenance shell during exactly the ingest it guards"),
    (("run", "--rm", "hermes", "--entrypoint", "bash"), True,
     "--entrypoint AFTER the service is an argument to the service's own "
     "command, so s6 still boots -- a substring check for the flag passed this"),
    (("run", "--rm", "-e", "--entrypoint", "hermes"), True,
     "--entrypoint as another flag's VALUE is not an entrypoint override"),
])
def test_the_veto_sees_every_subcommand_that_is_not_on_the_safe_list(
        run, instance, tmp_path, args, refused, why):
    """One table rather than four near-identical bodies: the contract IS a table
    of subcommand -> passes or asks the veto, and the next probe should cost a
    row."""
    _guarded(instance, run, tmp_path, refuses=True)
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    r = run("compose", "rowan", *args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    if refused:
        assert r.returncode != 0, why
        # Either gate may be the one that stops it: the veto for a transition,
        # the entrypoint check for a `run` that would boot a second gateway.
        assert "refused" in r.stderr or "without --entrypoint before the service" in r.stderr
    else:
        assert r.returncode == 0, f"{why}: {r.stderr}"


def test_the_suite_cannot_reach_a_docker_it_did_not_install(run, instance, tmp_path):
    """The harness fence, and it belongs in this file: restarting a live gateway
    is this repo's own failure class, and for a day the SUITE was the actor --
    `up`/`restore` on a fixture agent named `rowan` resolved to the production
    compose project, so 20 real `compose restart hermes` calls went out per run
    (plow-pbc/agent-mgr#13).

    A lifecycle command with no stub installed must be refused by the fixture's
    docker rather than reaching the daemon. `which docker` is asserted too: the
    session fixture shadows the real binary, and a test that rebuilt PATH from
    os.environ used to re-admit it."""
    import shutil
    run("register", "rowan", str(instance("rowan")))

    r = run("up", "rowan")
    assert r.returncode != 0, "a lifecycle command reached a docker nothing stubbed"
    assert "did not stub" in r.stderr

    # Process level, so it is red the moment the session fixture stops
    # shadowing -- deleted, un-autoused, or REAL_PATH captured after the
    # mutation. Nothing else in the suite would notice: once the fakes are
    # installed everything goes green again and the class reopens invisibly.
    import subprocess
    found = shutil.which("docker")
    assert found, "no docker on PATH at all"
    probe = subprocess.run([found, "info"], capture_output=True, text=True)
    assert probe.returncode == 97, f"{found} is the real docker, not the stub"
    assert "refusing a docker call a test did not stub" in probe.stderr


def test_a_container_that_mounts_someone_elses_home_is_not_touched(run, instance, tmp_path):
    """The hazard that is NOT test-only, and the reason this check is in the tool
    rather than in a fixture.

    AGENT_PROJECT derives from the agent NAME and Docker's namespace is global,
    so `agent-mgr restore rowan` from a scratch checkout addresses `-p
    hermes-rowan` -- production -- however thoroughly HOME and the registry are
    isolated. Every other check compares the descriptor against the config
    agent-mgr WOULD apply, and a scratch descriptor is perfectly self-consistent.
    An interactive run did exactly this to the live rentals gateway.

    Deriving the project from the name stays deliberate: two names may share one
    checkout so one repo can serve two people. So the container gets identified
    rather than the name constrained."""
    run("register", "rowan", str(instance("rowan")))
    log = tmp_path / "argv"
    foreign = tmp_path / "someone-else" / ".hermes-rowan"
    foreign.mkdir(parents=True)
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    mount=str(foreign), log=log)
    for cmd in ("restart", "up", "down"):
        r = run(cmd, "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
        assert r.returncode != 0, f"{cmd} touched a container mounting a different home"
        assert "not rowan's home" in r.stderr
        assert str(foreign) in r.stderr, "the message must name what it found"
        # The foreign home exists, so it belongs to a running agent: the remedy
        # must point at THIS descriptor, never at destroying that container.
        assert "docker rm -f" not in r.stderr, (
            "offered to destroy a live gateway the refused command would only have bounced")
        assert "unregister" in r.stderr, "no escape named, so the owner is locked out"
        assert "docker inspect" in r.stderr, "no way to find out whose it is"
    # The ORDER is the invariant, not just the exit code: checking after the call
    # would leave every assertion above green with the restart already sent to
    # the live project.
    argv = log.read_text()
    for mutating in ("restart", " up ", " down "):
        assert mutating not in argv, f"a {mutating.strip()} reached compose before the check"


def test_a_container_that_cannot_be_identified_is_refused(run, instance, tmp_path):
    """The other half of not-fail-open: a container exists and docker will not
    say whose home it mounts. We are about to touch something we cannot
    identify, so this refuses rather than assuming it is ours."""
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    d = b / "docker"
    d.write_text(d.read_text().replace('*inspect*) echo', '*inspect*) exit 3 ;; *never*) echo'))
    r = run("restart", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, "touched a container docker could not identify"
    assert "could not say whose home it mounts" in r.stderr


def test_no_state_of_the_foreign_mount_produces_a_removal_command(run, instance, tmp_path):
    """The discriminator that looked obvious -- does the foreign home exist? --
    runs as the invoking user on THIS host, while the mount is a path on the
    docker host owned by whoever runs that agent. Another user's home is
    unstattable under a default 750, and this tool supports one repo serving two
    people, so a live gateway would routinely read as nobody's. An empty mount
    lands in the same place. The asymmetry is total, so no state offers removal."""
    run("register", "rowan", str(instance("rowan")))
    for mount in (str(tmp_path / "nothing-here"), "/home/other/.hermes-rowan", ""):
        b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                        mount=mount)
        r = run("restart", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
        assert r.returncode != 0, f"touched a container mounting {mount!r}"
        assert "docker rm -f" not in r.stderr, f"offered removal for mount {mount!r}"
        assert "docker inspect" in r.stderr
