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
