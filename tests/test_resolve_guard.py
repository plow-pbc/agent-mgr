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


def _retargeting(instance, run, name, tmp_path):
    repo = instance(name)
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
    _retargeting(instance, run, "property", tmp_path)
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
