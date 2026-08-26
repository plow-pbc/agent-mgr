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
import subprocess
import tempfile
# Bound at import time, on purpose: collection does this before any fixture
# runs, so this name keeps pointing at the ORIGINAL run however `subprocess.run`
# is later rebound. See the test that uses it.
from subprocess import run as prebound_run
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
    (["run", "--entrypoint", "bash", "--rm", "hermes", "-c", "true"], True, "--entrypoint bash"),
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


@pytest.mark.parametrize("args, refused, why, expect_msg", [
    (("scale", "hermes=0"), True,
     "scale stops a container and was in neither list when the guard enumerated "
     "stoppers -- naming what is SAFE means an unknown subcommand asks the veto",
     "refused"),
    (("wait", "hermes", "--down-project"), True,
     "wait --down-project drops the whole project: membership of the safe list "
     "has to hold under every flag the subcommand accepts", "refused"),
    (("--project-name", "logs", "down"), True,
     "scanning the argv for the first recognised word let a global option's "
     "VALUE stand in for the subcommand -- it is $1 now, and a leading global "
     "is refused outright so nothing can shift it", "subcommand must come first"),
    (("run", "--entrypoint", "bash", "--rm", "hermes"), False,
     "a throwaway container beside the live one stops nothing, and refusing it "
     "would break the maintenance shell during exactly the ingest it guards", ""),
    (("run", "--rm", "--entrypoint", "bash", "hermes"), True,
     "not first: locating the service to check 'before it' needs a complete "
     "list of value-taking flags, and a missing entry admits a second gateway",
     "first argument is not --entrypoint"),
    # With a SERVICE present, so the invocation would really boot a container --
    # a bare `run --entrypoint` errors in docker on its own and cannot tell a
    # working guard from a broken one.
    (("run", "--entrypoint", "", "--rm", "hermes"), True,
     "first, but with no value: it overrides nothing and s6 still boots",
     "has no value"),
    (("run", "--entrypoint=", "--rm", "hermes"), True,
     "the same, spelled with =", "has no value"),
    (("run", "--entrypoint=bash", "--rm", "hermes"), False,
     "the = spelling WITH a value is a real override and must be admitted -- "
     "the only arm no row reached", ""),
    (("run", "--rm", "hermes", "--entrypoint", "bash"), True,
     "--entrypoint AFTER the service is an argument to the service's own "
     "command, so s6 still boots -- a substring check for the flag passed this",
     "first argument is not --entrypoint"),
    (("run", "--rm", "-e", "--entrypoint", "hermes"), True,
     "--entrypoint as another flag's VALUE is not an entrypoint override",
     "first argument is not --entrypoint"),
])
def test_the_veto_sees_every_subcommand_that_is_not_on_the_safe_list(
        run, instance, tmp_path, args, refused, why, expect_msg):
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
        # Each row asserts ITS refusal, not any refusal: widening the accepted
        # set let the global-option row pass on a message it was not written
        # for, which is the row stopping being exercised rather than passing.
        assert expect_msg in r.stderr, f"refused, but not for the reason under test: {r.stderr}"
    else:
        assert r.returncode == 0, f"{why}: {r.stderr}"


@pytest.mark.parametrize("path, why", [
    ("{outside}", "built from scratch, dropping the shadow entirely"),
    ("{outside}{sep}{inherited}", "inherits the shadow but resolves ahead of it"),
])
def test_an_override_that_reaches_the_real_docker_is_refused(run, path, why):
    """The companion to the fence below: it proves the stub refuses an unstubbed
    call, this proves an override cannot resolve docker back to the real one.

    The session fixture shadows the real docker by prepending to os.environ, so
    it survives the usual override -- f"{mybin}:{os.environ['PATH']}" -- and not
    the two shapes below. `reload-if-running` was already being invoked with the
    first; harmless only because that call leaves AGENT_MGR_ROOT unset and bails
    before compose. Left as a convention the next one is silent, and silent here
    means a green suite restarting a live gateway.

    The second row is why the check asks which docker the env RESOLVES, not
    whether the shadow is present: that PATH keeps the shadow in the list and
    still finds the one ahead of it.
    """
    # `ls`, not `restore`: if this fence ever regresses it must run something
    # that cannot reach a transition. `restore` ends in reload-if-running --
    # `compose restart` against -p hermes-rowan -- and "inert only because it
    # dies earlier" is the accident this whole change exists to stop relying on.
    # A docker outside the suite's tmp root, which is the whole predicate --
    # built here rather than taken from the host so the fence means the same
    # thing on a machine with docker somewhere else, or with none at all. The
    # assert fires before the spawn, so this never executes.
    with tempfile.TemporaryDirectory() as outside:
        _docker_outside_the_suite(outside)
        env = {"PATH": path.format(outside=outside, sep=os.pathsep,
                                   inherited=os.environ["PATH"])}
        # `why` carried into BOTH failure modes rather than dangling beside the
        # call: a bare DID NOT RAISE names only the parametrize id.
        try:
            run("ls", env=env)
        except AssertionError as refusal:
            assert "the suite did not create" in str(refusal), why
        else:
            pytest.fail(f"this PATH was not refused -- {why}")


def _docker_outside_the_suite(d):
    (Path(d) / "docker").write_text("#!/bin/sh\nexit 0\n")
    (Path(d) / "docker").chmod(0o755)
    return d


def test_an_explicit_env_with_no_path_is_refused():
    """The `run` fixture always builds a PATH, so no row above can hand the guard
    an env lacking the key -- this has to be spawned directly.

    The guard treats a missing key and an empty one identically. What this pins
    is the CHILD's behaviour, which does not: started with PATH unset, it falls
    back to the shell's own default and finds the operator's docker -- so an env
    that looks inert is the opposite.
    """
    with pytest.raises(AssertionError, match="carries no PATH"):
        subprocess.run([str(ROOT / "agent-mgr"), "ls"], env={})


def test_the_guard_sees_an_entry_point_bound_before_it_was_installed():
    """Why the guard wraps Popen and not run.

    `prebound_run` was bound at this module's import -- which collection does
    before any fixture runs -- so it keeps pointing at the original function
    however `subprocess.run` is later rebound. It reaches the daemon through
    the module-global Popen, which is why wrapping THAT catches it and
    wrapping `run` does not.

    Written after the first attempt at this test used `check_output`, which
    proved nothing: check_output calls `run` by name, so a run-only patch
    intercepts it just as well.
    """
    with tempfile.TemporaryDirectory() as outside:
        _docker_outside_the_suite(outside)
        with pytest.raises(AssertionError, match="the suite did not create"):
            prebound_run([str(ROOT / "agent-mgr"), "ls"], env={"PATH": outside})


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


@pytest.mark.parametrize("args", [
    ("logs", "rowan"),
    ("agent", "rowan", "what's on today?"),
    ("sign-in", "rowan"),
    ("compose", "rowan", "exec", "hermes", "cat", "/opt/data/.env"),
    ("compose", "rowan", "cp", "./x", "hermes:/opt/data/"),
    ("compose", "rowan", "top"),
    ("compose", "rowan", "events"),
    ("compose", "rowan", "port", "hermes", "8080"),
])
def test_every_command_that_reaches_an_existing_container_identifies_it(
        run, instance, tmp_path, args):
    """Not just the ones that STOP it. `agent rowan "<prompt>"` would exec a turn
    inside production's gateway and answer into the live owners' channel;
    `compose rowan cp` writes into production's home; `logs` streams it. The
    transition seam covers none of these -- `logs` bypassed even the passthrough,
    calling compose straight from the dispatch table."""
    run("register", "rowan", str(instance("rowan")))
    # sign-in reads the INSTALLED config before it asks whether a gateway is
    # running, so without this it dies earlier than the check under test.
    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("model:\n  provider: openai-codex\n")
    log = tmp_path / "argv"
    b = fake_docker(tmp_path, home=home, name="rowan",
                    mount="/home/someone-else/.hermes-rowan", exec_output="x", log=log)
    r = run(*args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, f"{args[0]} reached a container mounting a different home"
    assert "not rowan's home" in r.stderr
    # Order, not just the exit code: checking afterwards would leave both
    # assertions above green with the command already sent to the live project.
    argv = log.read_text()
    for verb in ("logs", " cp ", "exec", "top", "events"):
        assert verb not in argv, f"{verb.strip()} reached compose before the check"


@pytest.mark.parametrize("sub", [
    ["config"], ["version"], ["ls"], ["images"], ["build"], ["push"], ["ps"],
    # `pull` still needs no live daemon -- it needs the flag that keeps it from
    # replacing a built image, which is a different property and was tested by
    # dropping this row rather than adapting it.
    ["pull", "--ignore-buildable"],
])
def test_a_subcommand_that_touches_no_container_needs_no_daemon(run, instance, tmp_path, sub):
    """The identification costs a `compose ps`, which needs a live daemon. Gating
    the whole leaves-it-running list would make `config` -- which never contacted
    one -- require it."""
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    mount="/home/someone-else/.hermes-rowan")
    r = run("compose", "rowan", *sub, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, f"{sub} was gated on a container it never touches: {r.stderr}"


def test_a_stopped_siblings_container_is_not_treated_as_absent(run, instance, tmp_path):
    """Five rounds gated more CALL SITES and none of them touched what the check
    looks at. It asked `ps --status running`, so a stopped sibling read as
    absent and `up` -- the one command that would adopt its project -- went
    through. A stopped container still owns the name."""
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    running=False, exists=True, mount="/home/someone-else/.hermes-rowan")
    r = run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, "adopted a stopped sibling's project"
    assert "not rowan's home" in r.stderr


def test_a_home_that_traverses_to_a_siblings_is_caught(run, instance, tmp_path):
    """The collision check compared paths lexically, so `$HOME/foo/../.hermes`
    and `$HOME/.hermes` -- one directory -- compared unequal and restore
    overwrote the sibling's config and credentials. Canonicalised now, which is
    what collapsing repeated slashes only looked like it was doing."""
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    run("register", "copycat",
        str(instance("copycat", descriptor="AGENT_HOME=$HOME/foo/../.hermes\n")))
    r = run("restore", "copycat")
    assert r.returncode != 0, "a traversing home reached a sibling's directory"
    assert "str is already registered there" in r.stderr


def test_a_foreign_container_is_caught_even_beside_our_own(run, instance, tmp_path):
    """`-a` includes the one-off containers `compose run` leaves behind, which
    this tool deliberately supports -- so identifying the FIRST one and trusting
    it checked an arbitrary member of the set. Our own one-off mounts our home
    and passes; a sibling's does not, and ordering must not decide which is
    seen."""
    run("register", "rowan", str(instance("rowan")))
    ours = str(tmp_path / "home" / ".hermes-rowan")
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    all_cids=("ourown", "theirs"),
                    mounts={"ourown": ours, "theirs": "/home/other/.hermes-rowan"})
    r = run("restart", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, "a foreign container was missed because ours came first"
    assert "/home/other/.hermes-rowan" in r.stderr


def test_a_home_symlinked_onto_another_disk_still_works(run, instance, tmp_path):
    """Putting agent state on the big disk is ordinary. Resolving symlinks while
    normalising `..` would rewrite AGENT_HOME to the target, which matches
    neither shape require_own_home accepts -- so every direct write would be
    refused for a perfectly conventional setup."""
    target = tmp_path / "srv" / "rowan"
    target.mkdir(parents=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".hermes-rowan").symlink_to(target)
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    r = run("restore", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, f"a symlinked home was refused: {r.stderr}"


def test_two_homes_aliasing_one_directory_through_a_symlink_collide(run, instance, tmp_path):
    """The shape check needs the home as DECLARED -- a home symlinked onto a
    bigger disk is ordinary. This check asks a different question, "is it the
    same directory", and two spellings reaching one directory through a symlink
    is the aliasing the loop exists to catch. Normalising both lexically fixed
    the first and reopened the second."""
    target = tmp_path / "srv" / "shared"
    target.mkdir(parents=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".hermes-rowan").symlink_to(target)
    run("register", "rowan", str(instance("rowan")))
    run("register", "copycat",
        str(instance("copycat", descriptor=f"AGENT_HOME={target}\n")))
    r = run("restore", "copycat")
    assert r.returncode != 0, "two descriptors reached one directory undetected"
    assert "rowan is already registered there" in r.stderr


def test_pull_may_not_take_a_service_this_host_builds(run, instance, tmp_path):
    """One of the two doors the build exemption rests on, not the whole of it.

    This closes the fetch agent-mgr itself could issue. resolve-guard closes the
    other -- Compose fetching on its own under a pull_policy that is not `never`
    or `build`, which a marker test showed the default and `missing` both do.
    Two attempts to derive the guarantee from the image NAME were wrong before
    either door was found: fetchability is not a property of the string."""
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    env = {"PATH": f"{b}:{os.environ['PATH']}"}

    r = run("compose", "rowan", "pull", env=env)
    assert r.returncode != 0, "a pull could have replaced a built image"
    assert "--ignore-buildable" in r.stderr
    # The tail too: it points at the OTHER door, and a message claiming this
    # refusal is the whole guarantee is the framing this branch retracted.
    assert "'pull_policy: never' (or 'build')" in r.stderr

    # `pull` is not the only door: up/run/create all take --pull always, which
    # is the same substitution by a different route.
    for args in (("up", "-d", "--pull", "always"), ("up", "-d", "--pull=always"),
                 # The flag AFTER the service: only run and exec hand later
                 # words to the container, so only they may stop scanning there.
                 ("up", "hermes", "--pull", "always"),
                 # The CLI flag overrides a safe file-level policy, so it gets
                 # the same allowlist: `missing` fetches when the tag is absent.
                 ("up", "-d", "--pull", "missing"),
                 ("up", "-d", "--pull=missing"),
                 ("create", "hermes", "--pull=always"),
                 ("run", "--entrypoint", "bash", "--rm", "--pull", "always", "hermes")):
        r = run("compose", "rowan", *args, env=env)
        assert r.returncode != 0, f"{args} fetched past the guard"
        assert "could replace a built image" in r.stderr

    assert run("compose", "rowan", "pull", "--ignore-buildable",
               env=env).returncode == 0, "the safe form was refused too"
    for safe in (("up", "-d", "--pull", "never"), ("up", "-d", "--pull=build")):
        assert run("compose", "rowan", *safe, env=env).returncode == 0, (
            f"{safe} names a policy that does not fetch and was refused")

    # Keyed on the SUBCOMMAND and on flags before the service, per this file's
    # own rule -- scanning the whole argv made a container's own command line
    # trip the guard.
    assert run("compose", "rowan", "exec", "hermes", "git", "pull",
               env=env).returncode == 0, "a container's `git pull` tripped the fetch guard"

    # A leading global option would shift the subcommand out from under every
    # check that reads it as $1 -- including the fetch guard.
    r = run("compose", "rowan", "--project-name", "x", "pull", env=env)
    assert r.returncode != 0, "a global option carried a pull past the subcommand key"
    assert "subcommand must come first" in r.stderr

    # And a flag the old value-list had never heard of, with the fetch after it.
    r = run("compose", "rowan", "up", "-d", "--scale", "hermes=1", "--pull", "always", env=env)
    assert r.returncode != 0, "an unlisted value-taking flag truncated the scan"
    assert "could replace a built image" in r.stderr
