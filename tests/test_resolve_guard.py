"""The guard proves what Compose actually resolved, rather than trusting that
unsetting the descriptor keys held.

The refusal cases stub the mismatch rather than producing it through a real
`compose.override.yml`. What is under test is the guard's reaction to Compose
disagreeing with the descriptor, not Compose's merge -- and the suite runs with
the real docker shadowed, because a fixture agent named `rowan` or `str`
resolves to the LIVE compose project (plow-pbc/agent-mgr#13).
"""
import os

from conftest import fake_docker


def _mismatched(tmp_path, name, **kw):
    """A docker whose resolved config disagrees with the descriptor."""
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, **kw)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _agent(run, instance, name, descriptor=""):
    repo = instance(name, descriptor=descriptor)
    run("register", name, str(repo))
    return repo


def test_the_guard_passes_when_the_resolved_config_matches(run, instance):
    _agent(run, instance, "rowan")
    r = run("resolve-guard", "rowan")
    assert r.returncode == 0, r.stderr + r.stdout


def test_the_guard_refuses_when_an_override_retargets_the_home(run, instance, tmp_path):
    """An override that mounts a different home at /opt/data must be caught, even
    though every descriptor variable resolved exactly as written."""
    _agent(run, instance, "rowan")
    env = _mismatched(tmp_path, "rowan")
    # The mismatch: Compose resolves a different home at /opt/data.
    (tmp_path / "bin" / "docker").write_text(
        (tmp_path / "bin" / "docker").read_text().replace(
            str(tmp_path / "home" / ".hermes-rowan"), str(tmp_path / ".hermes-SOMEONE-ELSE")))
    r = run("resolve-guard", "rowan", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_the_guard_refuses_when_an_override_renames_the_container(run, instance, tmp_path):
    _agent(run, instance, "rowan")
    env = _mismatched(tmp_path, "rowan", container="hermes")
    r = run("resolve-guard", "rowan", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_a_stale_compose_project_name_cannot_re_project_the_stack(run, instance):
    """COMPOSE_PROJECT_NAME outranks the template's `name:`. container_name and
    the home both still resolve correctly, so only an explicit project check
    catches it -- and without it `up` builds a stack under a foreign project
    against this agent's live home while `down` reports nothing to stop."""
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve-guard", "rowan", env={"COMPOSE_PROJECT_NAME": "someone-elses-project"})
    assert r.returncode == 0, "the stale value must be unset, not merely detected: " + r.stderr


def test_an_override_cannot_re_project_the_stack_at_all(run, instance):
    """`-p` outranks every other source of the project name, so an override that
    sets `name:` is ignored rather than caught. Prevention, not detection: there
    is no path by which this agent's stack lands under another project."""
    repo = instance("rowan")
    run("register", "rowan", str(repo))
    (repo / "compose.override.yml").write_text("name: someone-elses-project\n")
    r = run("resolve-guard", "rowan")
    assert r.returncode == 0, r.stderr
    assert "someone-elses-project" not in run("resolve", "rowan").stdout


def _retargeting(instance, run, name, tmp_path, config=None):
    """Registered, and with a docker that resolves someone else's home."""
    repo = instance(name) if config is None else instance(name, config=config)
    run("register", name, str(repo))
    b = fake_docker(tmp_path, home=tmp_path / ".hermes-SOMEONE-ELSE", name=name)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def test_sign_in_will_not_write_a_credential_through_a_retargeting_override(run, instance, tmp_path):
    """sign-in mutates a running stack. Reaching Compose without the guard let a
    credential write land against a sibling agent's mounted home."""
    env = _retargeting(instance, run, "rowan", tmp_path)
    run("restore", "rowan")
    r = run("sign-in", "rowan", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_check_latch_will_not_probe_through_a_retargeting_override(run, instance, tmp_path):
    # A config that declares latch, so the probe gets past the not-configured
    # exit and actually reaches the guard this test is about.
    env = _retargeting(instance, run, "property", tmp_path,
                       config="model:\n  provider: openai-codex\nmcp_servers:\n  latch:\n"
                              "    url: https://api.plow.co/v1/relay/devices/x/mcp\n")
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_1\nDOMO_MCP_TOKEN=tok_1\n")
    r = run("check-latch", "property", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_the_guard_refuses_cleanly_when_compose_cannot_produce_a_config(run, instance, tmp_path):
    """A refusal, not a traceback: the operator needs to know the guard stopped
    them, not how it is implemented."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "docker").write_text("#!/usr/bin/env bash\necho 'not json'\nexit 0\n")
    (b / "docker").chmod(0o755)
    run("register", "rowan", str(instance("rowan")))
    import os
    r = run("resolve-guard", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "refusing to act" in r.stderr
    assert "Traceback" not in r.stderr


def _sibling_home(instance, run, name, tmp_path):
    """A descriptor copied from a sibling: self-consistent, and wrong."""
    repo = instance(name, descriptor=f"AGENT_HOME={tmp_path}/home/.hermes-rowan\n")
    run("register", name, str(repo))
    return repo


def test_restore_will_not_write_into_a_siblings_home(run, instance, tmp_path):
    """resolve-guard proves Compose agrees with the descriptor, which a copied
    descriptor naming a sibling's home satisfies perfectly. restore never goes
    near Compose, so only the ownership check catches it."""
    _sibling_home(instance, run, "property", tmp_path)
    r = run("restore", "property")
    assert r.returncode != 0
    assert "not property's own home" in r.stderr


def test_install_plugin_will_not_write_into_a_siblings_home(run, instance, tmp_path):
    _sibling_home(instance, run, "property", tmp_path)
    r = run("install-plugin", "property")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_add_skill_will_not_write_into_a_siblings_home(run, instance, tmp_path):
    _sibling_home(instance, run, "property", tmp_path)
    r = run("add-skill", "property", "plow-pbc/x", "--ref", "a" * 40, "--dest", "s")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_the_legacy_bare_home_is_still_allowed_when_declared(run, instance, tmp_path):
    """The rentals agent predates the convention; an explicit declaration is
    deliberate, and the convention can never produce a bare .hermes."""
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    r = run("restore", "str")
    assert r.returncode == 0, r.stderr


def test_two_agents_may_not_share_a_home(run, instance, tmp_path):
    """The check that actually closes the legacy exception. A descriptor copied
    from the rentals agent declares its bare `.hermes` and satisfies any
    name-shape test -- self-consistent and wrong. The registry sees it."""
    legacy = tmp_path / "home" / ".hermes"
    legacy.mkdir(parents=True, exist_ok=True)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    run("register", "copycat", str(instance("copycat", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    r = run("restore", "copycat")
    assert r.returncode != 0
    assert "str is already registered there" in r.stderr


def test_the_agent_that_declared_it_first_still_works(run, instance, tmp_path):
    (tmp_path / "home" / ".hermes").mkdir(parents=True, exist_ok=True)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    run("register", "rowan", str(instance("rowan")))
    assert run("restore", "str").returncode == 0


def test_sign_in_will_not_mint_into_a_siblings_home(run, instance, tmp_path):
    """It writes a credential into the home exactly as activate does."""
    run("register", "rowan", str(instance("rowan")))
    run("register", "property",
        str(instance("property", descriptor=f"AGENT_HOME={tmp_path}/home/.hermes-rowan\n")))
    r = run("sign-in", "property")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_a_siblings_single_quoted_home_still_collides(run, instance, tmp_path):
    """The collision check used to run its own descriptor parser, which stripped
    only double quotes -- so a sibling declaring AGENT_HOME='$HOME/.hermes'
    compared unequal to the same path and the collision it exists to catch went
    through. One resolver now, so both spellings resolve identically."""
    run("register", "str", str(instance("str", descriptor="AGENT_HOME='$HOME/.hermes'\n")))
    run("register", "rowan", str(instance("rowan", descriptor='AGENT_HOME="$HOME/.hermes"\n')))
    r = run("restore", "rowan")
    assert r.returncode != 0, "a second agent claimed a home a sibling already declares"
    assert "str is already registered there" in r.stderr



def test_an_unresolvable_sibling_does_not_open_the_legacy_home(run, instance, tmp_path):
    """The one arm that rests on the collision check having been complete. `str`
    owns the bare `.hermes`; move its repo and it stops resolving, so a copycat
    declaring the same home would otherwise pass and write config and
    credentials into a live agent's mounted home. Skipping the row silently
    turned the only fail-closed check here into a fail-open one."""
    import shutil
    str_repo = instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")
    run("register", "str", str(str_repo))
    shutil.rmtree(str_repo)
    run("register", "copycat", str(instance("copycat", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    r = run("restore", "copycat")
    assert r.returncode != 0, "a copycat claimed a live agent's home through a stale row"
    assert "could not be resolved" in r.stderr
    assert "could not resolve str" in r.stderr, "the skipped row was not named"



def test_the_legacy_owner_is_refused_by_a_stale_row_and_unregister_clears_it(
        run, instance, tmp_path):
    """The direction that reaches a deployed agent. `str` legitimately owns the
    bare `.hermes`; an unrelated dead row makes the collision check incomplete,
    so its own writes are refused -- and the way out has to actually work.
    `register` cannot serve: it refuses a directory that no longer exists, which
    is precisely the state the stale row is in."""
    import shutil
    dead = instance("dead")
    run("register", "dead", str(dead))
    shutil.rmtree(dead)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))

    r = run("restore", "str")
    assert r.returncode != 0, "the incomplete check did not refuse"
    assert "unregister" in r.stderr, "the refusal did not name a remedy that works"

    assert run("unregister", "dead").returncode == 0
    r = run("restore", "str")
    assert r.returncode == 0, f"unregister did not clear the refusal: {r.stderr}"


def test_an_unresolvable_sibling_refuses_every_shape_of_home(run, instance, tmp_path):
    """This replaces two tests that asserted a conventional home was unaffected
    by a stale row. That was true while the conventional name was self-proving,
    and stopped being true when the shape rule went lexical so a home symlinked
    onto a bigger disk keeps its declared name: `~/.hermes-rowan` and
    `~/.hermes-copycat` can now be two links to one directory and both pass the
    name test. An unresolvable sibling means the collision set is incomplete, so
    it can no longer be trusted for either shape.

    The cost is real -- one dead row refuses writes for every agent -- which is
    why the refusal names `unregister`, and why that command takes any row
    visible in `ls`.
    """
    import shutil
    dead = instance("dead")
    run("register", "dead", str(dead))
    shutil.rmtree(dead)
    run("register", "rowan", str(instance("rowan")))

    r = run("restore", "rowan")
    assert r.returncode != 0, "an incomplete collision set was trusted"
    assert "cannot prove no one else claims that home" in r.stderr
    assert "could not resolve dead" in r.stderr, "the skipped row was not named"
    assert "unregister" in r.stderr, "no escape named"

    assert run("unregister", "dead").returncode == 0
    assert run("restore", "rowan").returncode == 0, "unregister did not clear it"


def test_two_conventional_homes_aliasing_one_directory_collide(run, instance, tmp_path):
    """The case that invalidated the old invariant, on the shape where the
    collision loop is load-bearing: both names conventional, both symlinked to
    one directory, so the name test cannot tell them apart."""
    target = tmp_path / "srv" / "shared"
    target.mkdir(parents=True)
    home = tmp_path / "home"; home.mkdir(exist_ok=True)
    (home / ".hermes-rowan").symlink_to(target)
    (home / ".hermes-copycat").symlink_to(target)
    run("register", "rowan", str(instance("rowan")))
    run("register", "copycat", str(instance("copycat")))
    r = run("restore", "copycat")
    assert r.returncode != 0, "two conventional names reached one directory undetected"
    assert "rowan is already registered there" in r.stderr
