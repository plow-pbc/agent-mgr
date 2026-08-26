def test_ls_on_an_empty_registry_reports_no_agents(run):
    r = run("ls")
    assert r.returncode == 0
    assert "no agents registered" in r.stdout


def test_a_registered_agent_appears_in_ls(run, instance, tmp_path):
    repo = instance("property")
    assert run("register", "property", str(repo)).returncode == 0
    r = run("ls")
    assert "property" in r.stdout
    assert str(repo) in r.stdout


def test_registering_the_same_name_twice_updates_rather_than_duplicates(run, instance, tmp_path):
    a, b = instance("a"), instance("b")
    run("register", "property", str(a))
    run("register", "property", str(b))
    r = run("ls")
    assert r.stdout.count("property") == 1
    assert str(b) in r.stdout
    assert f"{a}\n" not in r.stdout


def test_registering_a_missing_directory_is_refused(run, tmp_path):
    r = run("register", "ghost", str(tmp_path / "nope"))
    assert r.returncode != 0
    assert "no such directory" in r.stderr


def test_an_invalid_agent_name_is_refused(run, instance, tmp_path):
    r = run("register", "Bad Name", str(instance("d")))
    assert r.returncode != 0
    assert "lowercase" in r.stderr


def test_an_unknown_subcommand_exits_nonzero_with_usage(run):
    r = run("frobnicate")
    assert r.returncode != 0
    assert "usage" in (r.stdout + r.stderr).lower()


def test_no_argument_at_all_prints_usage(run):
    r = run()
    assert r.returncode != 0
    assert "usage" in (r.stdout + r.stderr).lower()


def test_registering_a_directory_that_is_not_an_instance_repo_is_refused(run, tmp_path):
    """require_own_home iterates the whole registry and refuses on a row it
    cannot resolve, so one typo'd row would hard-fail every agent's writes."""
    d = tmp_path / "not-an-instance"
    d.mkdir()
    r = run("register", "typo", str(d))
    assert r.returncode != 0
    assert "not an instance repo" in r.stderr
    assert "typo" not in run("ls").stdout


def test_unregister_is_the_documented_way_out(run, instance):
    """The collision guard's refusal names this command; without it a deleted
    checkout blocks the fleet with no supported recovery."""
    run("register", "rowan", str(instance("rowan")))
    assert "rowan" in run("ls").stdout
    r = run("unregister", "rowan")
    assert r.returncode == 0, r.stderr
    assert "rowan" not in run("ls").stdout


def test_unregistering_an_unknown_agent_says_so(run):
    r = run("unregister", "ghost")
    assert r.returncode != 0
    assert "not registered" in r.stderr
