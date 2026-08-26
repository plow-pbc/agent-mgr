"""The guard proves what Compose actually resolved, rather than trusting that
unsetting the descriptor keys held."""


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
    repo = _agent(run, instance, "rowan")
    (repo / "compose.override.yml").write_text(
        "services:\n"
        "  hermes:\n"
        "    volumes:\n"
        f"      - {tmp_path}/.hermes-SOMEONE-ELSE:/opt/data\n"
    )
    r = run("resolve-guard", "rowan")
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_the_guard_refuses_when_an_override_renames_the_container(run, instance):
    repo = _agent(run, instance, "rowan")
    (repo / "compose.override.yml").write_text(
        "services:\n  hermes:\n    container_name: hermes\n")
    r = run("resolve-guard", "rowan")
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
    repo = instance(name) if config is None else instance(name, config=config)
    run("register", name, str(repo))
    (repo / "compose.override.yml").write_text(
        "services:\n"
        "  hermes:\n"
        "    volumes:\n"
        f"      - {tmp_path}/.hermes-SOMEONE-ELSE:/opt/data\n")
    return repo


def test_sign_in_will_not_write_a_credential_through_a_retargeting_override(run, instance, tmp_path):
    """sign-in mutates a running stack. Reaching Compose without the guard let a
    credential write land against a sibling agent's mounted home."""
    _retargeting(instance, run, "rowan", tmp_path)
    run("restore", "rowan")
    r = run("sign-in", "rowan")
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_check_latch_will_not_probe_through_a_retargeting_override(run, instance, tmp_path):
    # A config that declares latch, so the probe gets past the not-configured
    # exit and actually reaches the guard this test is about.
    _retargeting(instance, run, "property", tmp_path,
                 config="model:\n  provider: openai-codex\nmcp_servers:\n  latch:\n"
                        "    url: https://api.plow.co/v1/relay/devices/x/mcp\n")
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_1\nDOMO_MCP_TOKEN=tok_1\n")
    r = run("check-latch", "property")
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


def test_a_quoted_sibling_home_is_still_a_collision(run, instance, tmp_path):
    """The scan resolves through load_agent, not a second parser. A sibling
    writing AGENT_HOME='$HOME/.hermes' stayed quoted under the sed/strip
    version, compared unequal to the same resolved path, and the collision went
    unseen -- letting restore or sign-in write into that sibling's live home."""
    (tmp_path / "home" / ".hermes").mkdir(parents=True, exist_ok=True)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME='$HOME/.hermes'\n")))
    run("register", "copycat", str(instance("copycat", descriptor='AGENT_HOME="$HOME/.hermes"\n')))
    r = run("restore", "copycat")
    assert r.returncode != 0
    assert "str is already registered there" in r.stderr


def test_a_symlinked_home_cannot_borrow_a_siblings_name(run, instance, tmp_path):
    """The suffix rule is satisfied by the NAME, so `.hermes-copycat` pointing at
    `.hermes-rowan` passed it while writing into rowan's live home. Compared as
    filesystem identity now, not as strings."""
    home = tmp_path / "home"
    (home / ".hermes-rowan").mkdir(parents=True, exist_ok=True)
    (home / ".hermes-copycat").symlink_to(home / ".hermes-rowan")
    run("register", "rowan", str(instance("rowan")))
    run("register", "copycat", str(instance("copycat")))
    r = run("restore", "copycat")
    assert r.returncode != 0
    # The specific message: "refusing to write" is emitted by the shape rule
    # too, so a substring match cannot tell which guard fired -- and this case
    # is about identity, which only the collision loop compares.
    assert "rowan is already registered there" in r.stderr


def test_a_traversing_home_is_the_same_home(run, instance, tmp_path):
    """`$HOME/tmp/../.hermes-rowan` differs lexically and is the same directory."""
    (tmp_path / "home" / ".hermes-rowan").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "tmp").mkdir(parents=True, exist_ok=True)
    run("register", "rowan", str(instance("rowan")))
    run("register", "copycat",
        str(instance("copycat", descriptor="AGENT_HOME=$HOME/tmp/../.hermes-rowan\n")))
    r = run("restore", "copycat")
    assert r.returncode != 0
    assert "rowan is already registered there" in r.stderr


def test_a_symlinked_home_at_the_conventional_name_is_allowed(run, instance, tmp_path):
    """The shape rule validates the DECLARED path, because that is what gets
    written -- and a symlink at the conventional name is the only way an
    operator can relocate a home onto another volume. Canonicalizing here
    refused that, with a message naming the path that IS the agent's own home."""
    volume = tmp_path / "mnt" / "rowan"
    volume.mkdir(parents=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".hermes-rowan").symlink_to(volume)
    run("register", "rowan", str(instance("rowan")))
    r = run("restore", "rowan")
    assert r.returncode == 0, r.stderr
    assert (volume / "config.yaml").exists(), "the write did not follow the relocation"


def test_the_shape_rule_refuses_an_unrelated_home(run, instance):
    """Reaches `case "$AGENT_HOME"` -- no sibling is registered there, so the
    collision loop passes and this is the guard that fires. Without a case that
    gets past the loop, reverting the shape line leaves the file green."""
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_HOME=/tmp/somewhere-else\n")))
    r = run("restore", "rowan")
    assert r.returncode != 0
    assert "that is not rowan's own home" in r.stderr


def test_the_portable_realpath_fallback_agrees_with_the_gnu_one(run, instance, tmp_path):
    """realpath -m is GNU-only; BSD/macOS fails outright on a path that does not
    exist, which is every first restore. The fallback has to be exercised
    somewhere other than the machine where it would first break."""
    (tmp_path / "home" / ".hermes-rowan").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / ".hermes-copycat").symlink_to(tmp_path / "home" / ".hermes-rowan")
    run("register", "rowan", str(instance("rowan")))
    run("register", "copycat", str(instance("copycat")))
    r = run("restore", "copycat", env={"AGENT_MGR_FORCE_PORTABLE_REALPATH": "1"})
    assert r.returncode != 0
    assert "rowan is already registered there" in r.stderr
