"""An agent's home is the volume half of the fleet: repo = image, home = volume.

Nothing backed one up. Four homes on this host held 1.5 GB of live state --
auth.json, the dotenv, sessions, memories, kanban -- with zero copies anywhere,
and every home's own backups/ directory existed and was empty. This is the
command that fills them.
"""
import tarfile


def registered(run, instance, name, **kw):
    """Register an agent and create its home, the way restore would."""
    repo = instance(name, **kw)
    assert run("register", name, str(repo)).returncode == 0
    home = _home(run, name)
    home.mkdir(parents=True, exist_ok=True)
    return repo, home


def _home(run, name):
    import pathlib
    line = next(
        ln for ln in run("resolve", name).stdout.splitlines()
        if ln.startswith("AGENT_HOME=")
    )
    return pathlib.Path(line.split("=", 1)[1])


def members(archive):
    with tarfile.open(archive) as t:
        return {m.name for m in t.getmembers()}


def test_a_backup_carries_the_files_that_make_the_agent_itself(run, instance, tmp_path):
    """The dotenv and auth.json are the whole reason a home is not reproducible
    from git: restore can rebuild config.yaml, and nothing can rebuild these."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("PLOW_CHAT_TOKEN=x\n")
    (home / "auth.json").write_text("{}\n")
    (home / "memories").mkdir()
    (home / "memories" / "note.md").write_text("remembered\n")
    dest = tmp_path / "backups"
    dest.mkdir()

    r = run("backup", "rowan", env={"AGENT_MGR_BACKUP_DIR": str(dest)})
    assert r.returncode == 0, r.stderr

    archives = list(dest.glob("*.tar.gz"))
    assert len(archives) == 1, f"expected one archive, got {archives}"
    inside = members(archives[0])
    assert any(m.endswith("/.env") for m in inside)
    assert any(m.endswith("/auth.json") for m in inside)
    assert any(m.endswith("/memories/note.md") for m in inside)


def test_the_regenerable_bulk_is_left_out(run, instance, tmp_path):
    """logs/, cache/ and lazy-packages/ are most of the 1.5 GB and none of the
    value: a home restored without them is the same agent, and a backup that
    carries them is one nobody keeps enough copies of."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("PLOW_CHAT_TOKEN=x\n")
    for noise in ("logs", "cache", "lazy-packages"):
        (home / noise).mkdir()
        (home / noise / "bulk").write_text("x" * 1024)
    dest = tmp_path / "backups"
    dest.mkdir()

    assert run("backup", "rowan", env={"AGENT_MGR_BACKUP_DIR": str(dest)}).returncode == 0
    inside = members(next(iter(dest.glob("*.tar.gz"))))
    for noise in ("logs", "cache", "lazy-packages"):
        assert not any(f"/{noise}/" in m for m in inside), f"{noise} was carried"


def test_no_destination_is_refused_rather_than_guessed(run, instance, tmp_path):
    """Writing a backup somewhere the operator did not name is how a backup ends
    up on the same disk as the thing it is backing up."""
    registered(run, instance, "rowan")
    r = run("backup", "rowan", env={"AGENT_MGR_BACKUP_DIR": ""})
    assert r.returncode != 0
    assert "AGENT_MGR_BACKUP_DIR" in r.stderr


def test_a_home_that_does_not_exist_is_refused(run, instance, tmp_path):
    """A silent zero-byte archive of a missing home is worse than no archive:
    it makes the timer look healthy while backing up nothing."""
    repo = instance("ghost")
    assert run("register", "ghost", str(repo)).returncode == 0
    dest = tmp_path / "backups"
    dest.mkdir()
    r = run("backup", "ghost", env={"AGENT_MGR_BACKUP_DIR": str(dest)})
    assert r.returncode != 0
    assert "run 'agent-mgr restore ghost'" in r.stderr
    assert not list(dest.glob("*.tar.gz"))


def test_all_covers_every_registered_agent(run, instance, tmp_path):
    """One row missed is one agent with no copy, and the miss is invisible --
    which is the failure mode a per-name loop in a shell script actually has."""
    for name in ("rowan", "life", "property"):
        _, home = registered(run, instance, name)
        (home / ".env").write_text("x\n")
    dest = tmp_path / "backups"
    dest.mkdir()

    r = run("backup", "--all", env={"AGENT_MGR_BACKUP_DIR": str(dest)})
    assert r.returncode == 0, r.stderr
    assert len(list(dest.glob("*.tar.gz"))) == 3


def test_all_reports_the_agents_it_could_not_back_up_and_exits_nonzero(run, instance, tmp_path):
    """Keep going, then fail loudly. Aborting on the first bad row would leave
    the healthy agents unbacked-up too, and exiting 0 would hide the gap."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("x\n")
    assert run("register", "ghost", str(instance("ghost"))).returncode == 0
    dest = tmp_path / "backups"
    dest.mkdir()

    r = run("backup", "--all", env={"AGENT_MGR_BACKUP_DIR": str(dest)})
    assert r.returncode != 0
    assert "ghost" in r.stderr
    assert len(list(dest.glob("*.tar.gz"))) == 1, "rowan was skipped because ghost failed"


def test_retention_prunes_only_this_agents_own_archives(run, instance, tmp_path):
    """The destination is an operator-named directory that may hold anything, so
    the prune matches the exact names this command writes and nothing else --
    a bare `find -delete` over a directory the operator chose is a delete
    primitive pointed at their data."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("x\n")
    dest = tmp_path / "backups"
    dest.mkdir()
    stale = dest / ".hermes-rowan-20200101.tar.gz"
    stale.write_text("old")
    bystander = dest / "important.tar.gz"
    bystander.write_text("not ours")
    other_agent = dest / ".hermes-life-20200101.tar.gz"
    other_agent.write_text("someone else's")

    r = run("backup", "rowan", "--keep", "1",
            env={"AGENT_MGR_BACKUP_DIR": str(dest)})
    assert r.returncode == 0, r.stderr
    assert not stale.exists(), "the agent's own stale archive should be pruned"
    assert bystander.exists(), "a file this command never wrote must survive"
    assert other_agent.exists(), "another agent's archive is not this run's to prune"
