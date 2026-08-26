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


@pytest.mark.parametrize("args, why", [
    (("run", "--rm", "hermes", "--entrypoint", "bash"),
     "--entrypoint after the service is an argument to the service's command, "
     "so s6 still boots -- a substring check for the flag passed this"),
    (("run", "--rm", "-e", "--entrypoint", "hermes"),
     "--entrypoint as another flag's VALUE is not an entrypoint override"),
])
def test_run_needs_the_entrypoint_before_the_service(run, instance, tmp_path, args, why):
    """The whole failure this tool exists to prevent, reached through the escape
    hatch: without a replaced entrypoint the image's s6 boots a second gateway
    against the live agent's home."""
    import os
    from conftest import fake_docker
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    r = run("compose", "rowan", *args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, why
    assert "without --entrypoint before the service" in r.stderr


def test_a_subcommand_this_tool_has_not_heard_of_asks_the_veto(run, instance, tmp_path):
    """`scale hermes=0` stops a container and was in neither list back when the
    guard enumerated stoppers. Naming what is SAFE instead means an unknown
    subcommand asks the veto rather than skipping it."""
    import os
    from conftest import fake_docker
    from test_install import _guarded
    _guarded(instance, run, tmp_path, refuses=True)
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    r = run("compose", "rowan", "scale", "hermes=0", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, "scale stopped a container past a refusing guard"
    assert "refused" in r.stderr


def test_a_global_options_value_cannot_stand_in_for_the_subcommand(run, instance, tmp_path):
    """Scanning the argv for the first recognised word let `--project-name logs`
    classify a later `down` as a read. The subcommand is $1 now."""
    import os
    from conftest import fake_docker
    from test_install import _guarded
    _guarded(instance, run, tmp_path, refuses=True)
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    r = run("compose", "rowan", "--project-name", "logs", "down",
            env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, "a down hid behind a global option's value"
    assert "refused" in r.stderr


def test_a_maintenance_run_is_not_refused_while_the_guard_is_refusing(run, instance, tmp_path):
    """The escape hatch's whole purpose, and what inverting the guard's list
    nearly broke: `run --entrypoint` starts a throwaway container beside the live
    one and stops nothing, so a nightly-ingest guard has no business refusing it.
    The dangerous half -- an unreplaced entrypoint -- is refused upstream."""
    import os
    from conftest import fake_docker
    from test_install import _guarded
    _guarded(instance, run, tmp_path, refuses=True)
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    r = run("compose", "rowan", "run", "--rm", "--entrypoint", "bash", "hermes",
            env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, f"a refusing guard blocked a maintenance shell: {r.stderr}"
