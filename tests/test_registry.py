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
def test_a_pattern_is_a_name_that_matches_nothing_not_a_wildcard(run, instance, tmp_path, name):
    """The registry compares the name column as a FIELD, so a pattern is just a
    name no row has. It used to reach a grep BRE in every function here: `.*`
    matched every tab-bearing row and `unregister` wrote an empty file -- the
    whole fleet registry gone, reported as success, unrecoverable because the
    registry is the only record of name to dir and the rewrite clobbers in
    place. `s.r` silently dropped `str` and `sar` together."""
    run("register", "str", str(instance("str")))
    run("register", "rowan", str(instance("rowan")))

    r = run("unregister", name)
    assert r.returncode != 0, f"unregister {name!r} matched something"
    rows = run("ls").stdout
    assert "str" in rows and "rowan" in rows, "a refused unregister dropped rows"

    # Same on the read path, which failed differently: `restore 's.r'` selected
    # str's ROW while deriving its home from the PATTERN, so the deploy landed
    # in ~/.hermes-s.r and reported success with the live agent untouched.
    r = run("restore", name)
    assert r.returncode != 0, f"restore {name!r} resolved to some other agent's row"
    assert not (tmp_path / "home" / f".hermes-{name}").exists(), "a phantom home was created"


@pytest.mark.parametrize("row", ["Property", "a\\tb"])
def test_a_hand_edited_row_can_still_be_dropped(run, instance, tmp_path, registry, row):
    """Hand-editing is the documented pre-unregister practice, so rows outside
    [a-z0-9-] exist in the wild. Gating removal on the name made them
    undroppable -- and an unresolvable one refuses the bare-home agent, so the
    remedy the refusal names has to work on exactly the row that caused it."""
    run("register", "rowan", str(instance("rowan")))
    with registry.open("a") as f:
        f.write(f"{row}\t/nonexistent/repo\n")
    assert row in run("ls").stdout

    # The backslash row is the one that proves ENVIRON over -v: awk's -v
    # processes escapes in the value, so `a\tb` would arrive as a tab and match
    # no row -- undroppable, by the very command that exists to drop it.
    # The LOOKUP path too, and the two messages differ in a way only ENVIRON can
    # produce: it resolves the row and dies on the missing dir, while -v never
    # finds the row at all and says it is not registered.
    assert "no longer exists" in run("resolve", row).stderr

    assert run("unregister", row).returncode == 0, f"{row!r} could not be dropped"
    assert row not in run("ls").stdout
    assert "rowan" in run("ls").stdout


def test_a_numeric_looking_name_is_not_the_same_row_as_another(run, instance, tmp_path):
    """awk compares numerically when both operands look numeric, so `007` and `7`
    would be one row: registering the second would drop the first, and looking up
    either could resolve the other's directory."""
    run("register", "007", str(instance("007")))
    run("register", "7", str(instance("7")))
    rows = run("ls").stdout
    assert "007" in rows and "\n7 " in rows.replace("007", "xxx"), f"a row was absorbed: {rows}"
    assert run("resolve", "7").stdout.count("AGENT_DIR=") == 1
    assert "/7-repo" in run("resolve", "7").stdout
    assert "/007-repo" in run("resolve", "007").stdout

    # And the rewrite side: a numeric compare would have unregister 7 drop 007,
    # which is the same door register 7 would have silently deleted it through.
    assert run("unregister", "7").returncode == 0
    assert "007" in run("ls").stdout
