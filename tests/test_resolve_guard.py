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
