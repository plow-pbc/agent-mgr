import pathlib

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


def test_an_overlay_sets_a_per_instance_value(run, instance, registry):
    run("register", "rowan", str(instance("rowan")))
    _overlay(registry, "rowan", "AGENT_TZ=America/Chicago\n")
    assert "AGENT_TZ=America/Chicago" in run("resolve", "rowan").stdout


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
    out = run("resolve", "rowan").stdout
    assert "/opt/hijack" not in out
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes-rowan" in out
    assert "AGENT_CONTAINER=hermes-rowan" in out
    assert "AGENT_PROJECT=hermes-rowan" in out


def test_an_overlay_is_read_never_executed(run, instance, registry):
    """Same contract as the descriptor: a value cannot reach $(...)."""
    run("register", "rowan", str(instance("rowan")))
    _overlay(registry, "rowan", "AGENT_TZ=$(touch /tmp/agent-mgr-overlay-pwned)\n")
    out = run("resolve", "rowan").stdout
    assert "AGENT_TZ=$(touch /tmp/agent-mgr-overlay-pwned)" in out
    assert not pathlib.Path("/tmp/agent-mgr-overlay-pwned").exists()


def test_an_overlay_expands_home_like_the_descriptor(run, instance, registry, tmp_path):
    """One parser, so the two files cannot drift into separate dialects."""
    run("register", "rowan", str(instance("rowan")))
    _overlay(registry, "rowan", 'AGENT_CONFIG=$HOME/elsewhere.yaml\n')
    assert f"AGENT_CONFIG={tmp_path / 'home'}/elsewhere.yaml" in run("resolve", "rowan").stdout
