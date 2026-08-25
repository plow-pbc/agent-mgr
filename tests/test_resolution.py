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
