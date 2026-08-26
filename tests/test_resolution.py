import os
import pathlib

import pytest

from conftest import fake_docker

def test_home_defaults_to_the_conventional_path(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes-rowan" in r.stdout
    assert "AGENT_CONTAINER=hermes-rowan" in r.stdout
    assert "AGENT_PROJECT=hermes-rowan" in r.stdout


def test_the_image_defaults_to_the_fleet_wide_pinned_digest(run, instance):
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan")
    assert "AGENT_IMAGE=nousresearch/hermes-agent@sha256:" in r.stdout


def test_a_descriptor_override_wins_over_the_convention(run, instance):
    repo = instance("str", descriptor="AGENT_HOME=/opt/legacy/.hermes\nAGENT_CONTAINER=hermes\n")
    run("register", "str", str(repo))
    r = run("resolve", "str")
    assert "AGENT_HOME=/opt/legacy/.hermes" in r.stdout
    assert "AGENT_CONTAINER=hermes" in r.stdout


def test_a_descriptor_may_expand_home(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")
    run("register", "str", str(repo))
    r = run("resolve", "str")
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes\n" in r.stdout


def test_a_stale_shell_variable_cannot_retarget_the_agent(run, instance):
    """Compose resolves shell env ahead of --env-file. The descriptor must win."""
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan", env={"AGENT_HOME": "/home/odio/.hermes-WRONG"})
    assert "/home/odio/.hermes-WRONG" not in r.stdout
    assert ".hermes-rowan" in r.stdout


def test_a_stale_shell_container_name_cannot_retarget_the_agent(run, instance):
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan", env={"AGENT_CONTAINER": "hermes"})
    assert "AGENT_CONTAINER=hermes-rowan" in r.stdout


def test_an_unregistered_agent_is_refused_by_name(run):
    r = run("resolve", "ghost")
    assert r.returncode != 0
    assert "ghost" in r.stderr and "not registered" in r.stderr


def test_an_instance_repo_with_no_descriptor_is_refused(run, tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    run("register", "bare", str(repo))
    r = run("resolve", "bare")
    assert r.returncode != 0
    assert "agent.env" in r.stderr


def test_resolve_with_no_name_asks_which_agent(run):
    r = run("resolve")
    assert r.returncode != 0
    assert "which agent" in r.stderr


def test_a_descriptor_is_read_not_executed(run, instance, tmp_path):
    """The descriptor is documented as declarative. Dot-sourcing it made any
    registered repo able to run arbitrary commands with the operator's
    credentials the moment `resolve` touched it -- before any Compose guard."""
    canary = tmp_path / "pwned"
    repo = instance("rowan", descriptor=f'AGENT_TZ=$(touch {canary})\n')
    run("register", "rowan", str(repo))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert not canary.exists(), "the descriptor executed a command substitution"
    assert "AGENT_TZ=$(touch" in r.stdout, "the value should stay literal"


def test_a_descriptor_cannot_run_a_command_through_a_later_line(run, instance, tmp_path):
    """A parser that only *starts* strict still executes if it falls back to
    sourcing on anything it does not recognise."""
    canary = tmp_path / "pwned2"
    repo = instance("rowan", descriptor=f'AGENT_TZ=UTC\ntouch {canary}\nAGENT_CONTAINER=x\n')
    run("register", "rowan", str(repo))
    r = run("resolve", "rowan")
    assert not canary.exists(), "a non-assignment line was executed"
    assert "AGENT_TZ=UTC" in r.stdout


def test_a_descriptor_cannot_set_the_loader_or_the_command_lookup(run, instance):
    """A PATH from a descriptor would reach every docker, curl and gh this tool
    runs afterwards -- the shell execution the parse exists to prevent, by a
    different door. Held by the allowlist rather than a denylist: only
    AGENT_KEYS reaches this process, so the dangerous names do not have to be
    enumerated in advance."""
    repo = instance("rowan", descriptor="PATH=/tmp/evil\nLD_PRELOAD=/tmp/x.so\nAGENT_TZ=UTC\n")
    run("register", "rowan", str(repo))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert "/tmp/evil" not in r.stdout
    assert "AGENT_TZ=UTC" in r.stdout, "the rest of the descriptor still applies"


def test_home_expansion_still_works_because_the_template_documents_it(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_HOME=${HOME}/.hermes\n")
    run("register", "str", str(repo))
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes\n" in run("resolve", "str").stdout


def test_an_instances_own_variables_reach_its_hook(run, instance, tmp_path):
    """An override-only variable like STR_VAULT is what an instance's compose
    override and its restore hook are written against. Exporting it here is what
    lets the hook stop keeping a second copy of a path the descriptor owns."""
    out = tmp_path / "seen"
    repo = instance("str", descriptor=f"STR_VAULT=$HOME/hermes-vault\nAGENT_RESTORE_HOOK=h.sh\n")
    hook = repo / "h.sh"
    hook.write_text(f'#!/usr/bin/env bash\nprintf "%s" "$STR_VAULT" > {out}\n')
    hook.chmod(0o755)
    run("register", "str", str(repo))
    r = run("restore", "str")
    assert r.returncode == 0, r.stderr
    assert out.read_text() == f"{tmp_path / 'home'}/hermes-vault"


def test_a_non_agent_variable_is_still_parsed_not_executed(run, instance, tmp_path):
    """Widening the parser past AGENT_* must not widen it into a shell."""
    canary = tmp_path / "pwned3"
    run("register", "str", str(instance("str", descriptor=f'STR_VAULT=$(touch {canary})\n')))
    r = run("resolve", "str")
    assert r.returncode == 0, r.stderr
    assert not canary.exists()


def test_a_descriptor_cannot_repoint_the_tool_at_its_own_code(run, instance, tmp_path):
    """AGENT_MGR_ROOT decides where lib/ is loaded from. Exporting every
    descriptor key handed a registered repo the dispatcher: ordinary lifecycle
    commands would run that repo's resolve-guard with the operator's
    credentials. An allowlist closes it; a denylist cannot, because the
    dangerous names are whatever this tool happens to read."""
    evil = tmp_path / "evil"
    (evil / "lib").mkdir(parents=True)
    canary = tmp_path / "pwned-root"
    (evil / "lib" / "resolve-guard").write_text(f"#!/usr/bin/env bash\ntouch {canary}\n")
    (evil / "lib" / "resolve-guard").chmod(0o755)
    run("register", "rowan", str(instance("rowan", descriptor=f"AGENT_MGR_ROOT={evil}\n")))
    import os
    from conftest import fake_docker
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    run("resolve", "rowan")
    # `up` runs resolve-guard, which is the file the descriptor tried to supply.
    run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert not canary.exists(), "a descriptor repointed AGENT_MGR_ROOT"


def test_an_instance_variable_still_reaches_its_hook(run, instance, tmp_path):
    """Narrowing the export must not take the hooks' own environment with it."""
    seen = tmp_path / "seen"
    repo = instance("str", descriptor="STR_VAULT=/tmp/v\nAGENT_PRE_TRANSITION=g.sh\n")
    g = repo / "g.sh"
    g.write_text(f'#!/usr/bin/env bash\nprintf "%s" "$STR_VAULT" > {seen}\n')
    g.chmod(0o755)
    run("register", "str", str(repo))
    import os
    from conftest import fake_docker
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-str", name="str",
                    container="hermes-str", project="hermes-str")
    run("up", "str", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert seen.read_text() == "/tmp/v"





# --- the per-instance overlay ------------------------------------------------
#
# What lets ONE agent repo serve several people. Everything else an instance
# needs already lives outside the tree; a tracked descriptor holds one value, so
# a per-person setting had nowhere to go.


def _overlay(registry, name, text):
    """Write ~/.config/agent-mgr/<name>.env, beside the registry."""
    registry.parent.mkdir(parents=True, exist_ok=True)
    (registry.parent / f"{name}.env").write_text(text)


def test_no_overlay_changes_nothing(run, instance, registry, tmp_path):
    """The absent case is the common one, and it must resolve identically."""
    run("register", "rowan", str(instance("rowan")))
    before = run("resolve", "rowan").stdout
    _overlay(registry, "someone-else", "AGENT_TZ=America/Chicago\n")
    assert run("resolve", "rowan").stdout == before


def test_the_overlay_beats_the_shared_descriptor(run, instance, registry):
    """The whole point: one repo, two instances, different zones."""
    repo = instance("rowan", descriptor="AGENT_TZ=America/Los_Angeles\n")
    run("register", "rowan", str(repo))
    _overlay(registry, "rowan", "AGENT_TZ=America/Chicago\n")
    out = run("resolve", "rowan").stdout
    assert "AGENT_TZ=America/Chicago" in out
    assert "America/Los_Angeles" not in out


def test_two_instances_of_one_repo_take_their_own_zones(run, instance, registry):
    """One checkout, two registry rows, two overlays -- the shape this exists for."""
    repo = str(instance("life"))
    run("register", "life", repo)
    run("register", "rowan", repo)
    _overlay(registry, "rowan", "AGENT_TZ=America/Chicago\n")
    life, rowan = run("resolve", "life").stdout, run("resolve", "rowan").stdout
    assert "AGENT_TZ=America/Los_Angeles" in life
    assert "AGENT_TZ=America/Chicago" in rowan
    assert "AGENT_HOME=" in life and "/.hermes-life" in life
    assert "/.hermes-rowan" in rowan


def test_an_overlay_cannot_retarget_identity(run, instance, registry, tmp_path):
    """Home, container and project are the REGISTRY NAME's to derive.

    An overlay is keyed by that same name, so a bad one would agree with itself:
    require_own_home asks whether AGENT_HOME ends in .hermes-$AGENT_NAME, which
    an overlay claiming a sibling's home passes only if it also renames itself.
    Excluding the keys is what closes it, not a later shape check.
    """
    run("register", "rowan", str(instance("rowan")))
    _overlay(registry, "rowan",
             "AGENT_HOME=/opt/hijack\nAGENT_CONTAINER=hermes\nAGENT_PROJECT=hermes\n")
    r = run("resolve", "rowan")
    assert "/opt/hijack" not in r.stdout
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes-rowan" in r.stdout
    assert "AGENT_CONTAINER=hermes-rowan" in r.stdout
    assert "AGENT_PROJECT=hermes-rowan" in r.stdout
    # Named, not dropped: a home that did not move looks identical to one never
    # set, so silence would leave an operator believing the overlay took effect.
    assert "may not set AGENT_HOME" in r.stderr


def test_an_overlay_cannot_set_a_non_identity_key_either(run, instance, registry):
    """The overlay is AGENT_TZ alone, so keys that were allowed before the
    narrowing are dropped too -- and they get a different sentence.

    AGENT_IMAGE has nothing to do with the registry name, so telling an operator
    that identity comes from it would point them at the wrong file to fix.
    """
    run("register", "rowan", str(instance("rowan")))
    _overlay(registry, "rowan", "AGENT_IMAGE=nousresearch/hermes-agent:pinned-by-hand\n")
    r = run("resolve", "rowan")
    assert "pinned-by-hand" not in r.stdout
    assert "AGENT_IMAGE=nousresearch/hermes-agent@sha256:" in r.stdout
    assert "may not set AGENT_IMAGE" in r.stderr
    assert "identity comes from the registry name" not in r.stderr
    assert "agent.env" in r.stderr


def test_an_overlay_is_read_never_executed(run, instance, registry):
    """Same contract as the descriptor: a value cannot reach $(...)."""
    run("register", "rowan", str(instance("rowan")))
    _overlay(registry, "rowan", "AGENT_TZ=$(touch /tmp/agent-mgr-overlay-pwned)\n")
    out = run("resolve", "rowan").stdout
    assert "AGENT_TZ=$(touch /tmp/agent-mgr-overlay-pwned)" in out
    assert not pathlib.Path("/tmp/agent-mgr-overlay-pwned").exists()


def test_resolve_shows_which_overlay_contributed(run, instance, registry):
    """`resolve` is the debugging surface for "why is this agent in Chicago".
    A second source of values it cannot show is worse than no second source."""
    run("register", "rowan", str(instance("rowan")))
    assert "AGENT_OVERLAY=\n" in run("resolve", "rowan").stdout
    _overlay(registry, "rowan", "AGENT_TZ=America/Chicago\n")
    assert f"AGENT_OVERLAY={registry.parent / 'rowan.env'}" in run("resolve", "rowan").stdout


def test_the_overlay_zone_reaches_compose_through_the_export(run, instance, registry, tmp_path):
    """The whole chain, not the second hop.

    `compose()` passes --env-file <the agent's own agent.env>, which in a shared
    repo declares somebody else's zone. The overlay wins only if load_agent
    EXPORTS the resolved AGENT_TZ (Compose reads shell variables ahead of
    --env-file). Two hops, and testing either alone is green while the other is
    broken: `resolve` reads ${!k} and never needs the export, and a rendering
    test that injects AGENT_TZ by hand never runs the export list at all.

    So this runs the real CLI against a stub `docker` that reports the
    AGENT_TZ it was handed -- dropping AGENT_TZ from the export list fails here
    and nowhere else.
    """
    repo = instance("rowan", descriptor="AGENT_TZ=America/Los_Angeles\n")
    run("register", "rowan", str(repo))
    _overlay(registry, "rowan", "AGENT_TZ=America/Chicago\n")

    seen = tmp_path / "seen-tz"
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    stub = b / "docker"
    stub.write_text(stub.read_text().replace(
        "#!/usr/bin/env bash",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "${{AGENT_TZ-<unset>}}" >> {seen}', 1))

    r = run("compose", "rowan", "config", env={"PATH": f"{b}:{os.environ['PATH']}"})
    # Bind and check: resolve-guard runs its own `compose config` in a separate
    # process BEFORE the passthrough, and it exports through the same
    # load_agent -- so every assertion below is already satisfied by the guard's
    # call. Without this, a dead guard or a broken passthrough leaves the test
    # green while the subcommand its docstring names never ran.
    assert r.returncode == 0, r.stderr
    assert seen.exists(), "the stub docker never ran -- the chain was not exercised"
    saw = seen.read_text()
    assert "America/Chicago" in saw, (
        f"compose was handed {saw.strip()!r}; the overlay's zone did not reach it, "
        f"so the container would run on the shared descriptor's clock"
    )
    assert "America/Los_Angeles" not in saw


@pytest.fixture
def injection_marker():
    """A marker path containing NO '-'.

    Load-bearing, not cosmetic. The exploit needs the key's bracket expression
    to be a VALID character class, and a '-' between two characters inside one
    is a range -- `[$(touch /tmp/pytest-of-odio/...)Z]` is an invalid range, so
    grep errors out and the attack silently fails to reproduce. pytest's
    tmp_path always contains '-', so a marker under it makes this test pass
    against the vulnerable code: green for the wrong reason, which is the whole
    defect class being fixed here.
    """
    d = pathlib.Path(f"/tmp/agentmgr_inj_{os.getpid()}")
    d.mkdir(exist_ok=True)
    marker = d / "executed"
    if marker.exists():
        marker.unlink()
    yield marker
    if marker.exists():
        marker.unlink()
    d.rmdir()


def test_a_descriptor_key_cannot_execute_host_code(run, instance, injection_marker):
    """Read, never execute -- the property this parser exists for.

    The membership check ran $key as a grep PATTERN, so a bracket expression
    could match an allowlisted name as a character class (`AGENT_T[...Z]`
    matches AGENT_TZ). The name then reached `printf -v`, where bash evaluates
    an array subscript arithmetically and arithmetic performs command
    substitution -- so any `agent-mgr resolve` on a registered repo ran that
    repo's code as the operator.
    """
    key = f"AGENT_T[$(touch {injection_marker})Z]"
    run("register", "rowan", str(instance("rowan", descriptor=f"{key}=1\n")))
    r = run("resolve", "rowan")
    assert not injection_marker.exists(), (
        "a descriptor key executed host code -- the parser is a shell again"
    )
    assert r.returncode == 0, r.stderr
    # Dropped, not smuggled in under the name it impersonated.
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout


def test_an_overlay_key_cannot_execute_host_code(run, instance, registry, injection_marker):
    """Same sink, second file -- the overlay goes through the same parser."""
    run("register", "rowan", str(instance("rowan")))
    _overlay(registry, "rowan", f"AGENT_T[$(touch {injection_marker})Z]=1\n")
    r = run("resolve", "rowan")
    assert not injection_marker.exists(), "an overlay key executed host code"
    assert r.returncode == 0, r.stderr
