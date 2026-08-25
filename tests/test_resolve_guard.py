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
