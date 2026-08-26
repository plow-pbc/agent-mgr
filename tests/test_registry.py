import pytest


def test_ls_on_an_empty_registry_reports_no_agents(run):
    r = run("ls")
    assert r.returncode == 0
    assert "no agents registered" in r.stdout


def test_a_registered_agent_appears_in_ls(run, tmp_path):
    repo = tmp_path / "sams-property-hermes-agent"
    repo.mkdir()
    assert run("register", "property", str(repo)).returncode == 0
    r = run("ls")
    assert "property" in r.stdout
    assert str(repo) in r.stdout


def test_registering_the_same_name_twice_updates_rather_than_duplicates(run, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
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


def test_an_invalid_agent_name_is_refused(run, tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    r = run("register", "Bad Name", str(d))
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


@pytest.mark.parametrize("name", [".*", ".", "[a-z]*", "s.r"])
def test_unregister_refuses_a_name_that_is_a_pattern(run, instance, tmp_path, name):
    """The registry is the only record of name->dir and the rewrite clobbers it
    in place, so a pattern here is unrecoverable: `.*` matches every tab-bearing
    row and writes an empty file. This is also the command the fail-closed
    refusal sends an operator to mid-incident, argument possibly glob-mangled."""
    run("register", "str", str(instance("str")))
    run("register", "rowan", str(instance("rowan")))
    r = run("unregister", name)
    assert r.returncode != 0, f"unregister {name!r} was accepted"
    assert "lowercase letters" in r.stderr
    rows = run("ls").stdout
    assert "str" in rows and "rowan" in rows, "rows were dropped by a refused unregister"

    # The read path interpolates too, and fails differently: `restore 's.r'`
    # matches str's ROW while deriving its home from the PATTERN, so the deploy
    # lands in ~/.hermes-s.r and reports success with the live agent untouched.
    r = run("restore", name)
    assert r.returncode != 0, f"restore {name!r} resolved to some other agent's row"
    assert "lowercase letters" in r.stderr
