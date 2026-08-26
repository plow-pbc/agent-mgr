"""An agent's home is the volume half of the fleet: repo = image, home = volume.

Nothing backed one up. Four homes on this host held 1.5 GB of live state --
auth.json, the dotenv, sessions, memories, kanban -- with zero copies anywhere,
and every home's own backups/ directory existed and was empty. This is the
command that fills them.
"""
import os
import tarfile

import pytest


@pytest.fixture
def backup_dir(tmp_path):
    """Every scenario needs one; ten of them built it two lines at a time."""
    dest = tmp_path / "backups"
    dest.mkdir()
    return dest


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


def test_the_archive_carries_the_irreplaceable_half_and_not_the_bulk(
        run, instance, tmp_path, backup_dir):
    """One contract, one test: what a restore must find, and what it must not
    carry. The dotenv and auth.json are the whole reason a home is not
    reproducible from git -- restore rebuilds config.yaml, nothing rebuilds
    these. logs/, cache/ and lazy-packages/ are most of the 1.5 GB and none of
    the value, and an archive carrying them is one nobody keeps enough copies
    of."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("PLOW_CHAT_TOKEN=x\n")
    (home / "auth.json").write_text("{}\n")
    (home / "memories").mkdir()
    (home / "memories" / "note.md").write_text("remembered\n")
    for noise in ("logs", "cache", "lazy-packages"):
        (home / noise).mkdir()
        (home / noise / "bulk").write_text("x" * 1024)

    r = run("backup", "rowan", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode == 0, r.stderr

    archives = list(backup_dir.glob("*.tar.gz"))
    assert len(archives) == 1, f"expected one archive, got {archives}"
    inside = members(archives[0])
    for kept in (".env", "auth.json", "memories/note.md"):
        assert any(m.endswith(kept) for m in inside), f"{kept} missing"
    for dropped in ("logs", "cache", "lazy-packages"):
        assert not any(f"/{dropped}/" in m for m in inside), f"{dropped} was carried"


def test_no_destination_is_refused_rather_than_guessed(run, instance, tmp_path, backup_dir):
    """Writing a backup somewhere the operator did not name is how a backup ends
    up on the same disk as the thing it is backing up."""
    registered(run, instance, "rowan")
    r = run("backup", "rowan", env={"AGENT_MGR_BACKUP_DIR": ""})
    assert r.returncode != 0
    assert "AGENT_MGR_BACKUP_DIR" in r.stderr


def test_a_home_that_does_not_exist_is_refused(run, instance, tmp_path, backup_dir):
    """A silent zero-byte archive of a missing home is worse than no archive:
    it makes the timer look healthy while backing up nothing."""
    repo = instance("ghost")
    assert run("register", "ghost", str(repo)).returncode == 0
    r = run("backup", "ghost", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode != 0
    assert "run 'agent-mgr restore ghost'" in r.stderr
    assert not list(backup_dir.glob("*.tar.gz"))


def test_all_covers_every_registered_agent(run, instance, tmp_path, backup_dir):
    """One row missed is one agent with no copy, and the miss is invisible --
    which is the failure mode a per-name loop in a shell script actually has."""
    for name in ("rowan", "life", "property"):
        _, home = registered(run, instance, name)
        (home / ".env").write_text("x\n")

    r = run("backup", "--all", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode == 0, r.stderr
    assert len(list(backup_dir.glob("*.tar.gz"))) == 3


def test_all_reports_the_agents_it_could_not_back_up_and_exits_nonzero(run, instance, tmp_path, backup_dir):
    """Keep going, then fail loudly. Aborting on the first bad row would leave
    the healthy agents unbacked-up too, and exiting 0 would hide the gap."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("x\n")
    assert run("register", "ghost", str(instance("ghost"))).returncode == 0

    r = run("backup", "--all", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode != 0
    assert "ghost" in r.stderr
    assert len(list(backup_dir.glob("*.tar.gz"))) == 1, "rowan was skipped because ghost failed"


def test_retention_prunes_only_this_agents_own_archives(run, instance, tmp_path, backup_dir):
    """The destination is an operator-named directory that may hold anything, so
    the prune matches the exact names this command writes and nothing else --
    a bare `find -delete` over a directory the operator chose is a delete
    primitive pointed at their data."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("x\n")
    stale = backup_dir / "rowan-20200101.tar.gz"
    stale.write_text("old")
    bystander = backup_dir / "important.tar.gz"
    bystander.write_text("not ours")
    other_agent = backup_dir / "life-20200101.tar.gz"
    other_agent.write_text("someone else's")

    r = run("backup", "rowan", "--keep", "1",
            env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode == 0, r.stderr
    assert not stale.exists(), "the agent's own stale archive should be pruned"
    assert bystander.exists(), "a file this command never wrote must survive"
    assert other_agent.exists(), "another agent's archive is not this run's to prune"


def test_archives_are_named_by_the_agent_not_its_home(run, instance, tmp_path, backup_dir):
    """require_own_home admits the legacy `*/.hermes` shape for any agent that
    declares it, and only refuses two agents resolving to the SAME directory.
    Named by the home's basename, two agents with distinct homes ending in the
    same component collapse onto one filename: --all overwrites the first with
    the second and exits 0, leaving an agent with no copy at all."""
    for name in ("rowan", "life"):
        repo = instance(name, descriptor=f"AGENT_HOME=$HOME/{name}-side/.hermes\n")
        assert run("register", name, str(repo)).returncode == 0
        home = _home(run, name)
        home.mkdir(parents=True)
        (home / ".env").write_text("x\n")

    r = run("backup", "--all", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode == 0, r.stderr
    written = sorted(p.name.split("-")[0] for p in backup_dir.glob("*.tar.gz"))
    assert written == ["life", "rowan"], f"the two homes collapsed onto {written}"


def test_all_on_an_empty_registry_is_refused_rather_than_silently_green(run, tmp_path, backup_dir):
    """A timer reporting success having archived nothing is the exact silent
    miss this command exists to end -- and an unreadable registry looks the
    same as an empty one, since registry_list swallows it."""
    r = run("backup", "--all", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode != 0
    assert "nothing was backed up" in r.stderr


def test_a_failed_archive_never_lands_under_a_valid_name(run, instance, tmp_path, backup_dir):
    """A run that dies partway must not destroy the previous good archive or
    leave a truncated one under a valid name — for the tool holding the only
    copy of unrebuildable state, a corrupt archive that looks healthy is the
    worst possible output. The failure is injected after staging succeeds; an
    unwritable destination instead fails `mktemp` before `tar` runs, staging
    nothing and pinning nothing."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("x\n")
    good = backup_dir / "rowan-20200101.tar.gz"
    good.write_text("yesterday's, still good")

    # A tar that writes a partial archive into the staged path, then dies the
    # way a real failure does (status 2, not the tolerated 1).
    shim = tmp_path / "shimbin"
    shim.mkdir()
    (shim / "tar").write_text(
        "#!/usr/bin/env bash\n"
        "for a in \"$@\"; do [ -n \"${take:-}\" ] && { printf 'partial' > \"$a\"; break; }; "
        "[ \"$a\" = -czf ] && take=1; done\n"
        "exit 2\n")
    (shim / "tar").chmod(0o755)

    r = run("backup", "rowan",
            env={"AGENT_MGR_BACKUP_DIR": str(backup_dir),
                 "PATH": f"{shim}:{os.environ['PATH']}"})

    assert r.returncode != 0, "tar status 2 is a real failure, not the tolerated warning"
    assert good.read_text() == "yesterday's, still good", "the previous good archive was destroyed"
    # The FULL set, not a negative glob carrying today's date: a glob that
    # stops matching anything the command can write is permanently true, and
    # the whole listing also catches a partial landing under any other stamp.
    assert [p.name for p in backup_dir.glob("rowan-*.tar.gz")] == ["rowan-20200101.tar.gz"], \
        "a partial archive was promoted"
    assert not list(backup_dir.glob(".rowan-*")), "the staged file was left behind"


def test_a_home_written_while_it_is_read_still_produces_an_archive(run, instance, tmp_path, backup_dir):
    """tar exits 1 for 'file changed as we read it', which a running gateway
    provokes on essentially every nightly run, and the archive it produced is
    complete. Treating that as failure makes the nightly cry wolf, which is how
    a real miss gets ignored. Status 2 stays a failure."""
    import subprocess
    import sys
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("x\n")
    big = home / "sessions.db"
    big.write_bytes(b"x" * (60 * 1024 * 1024))

    churn = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\n"
         "f = open(sys.argv[1], 'r+b')\n"
         "for _ in range(600):\n"
         "    f.seek(0); f.write(b'y' * 4096); f.flush(); time.sleep(0.002)\n",
         str(big)],
        env={"PATH": __import__("os").environ["PATH"]})
    try:
        r = run("backup", "rowan", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    finally:
        churn.kill()
        churn.wait()

    assert r.returncode == 0, r.stderr
    archive = next(iter(backup_dir.glob("*.tar.gz")))
    assert any(m.endswith("/.env") for m in members(archive))


def test_a_home_symlinked_onto_another_disk_is_actually_archived(
        run, instance, tmp_path, backup_dir):
    """A home symlinked onto a bigger disk is supported: load_agent normalises
    rather than canonicalises for exactly that. Archiving from the home's
    PARENT stores that top-level symlink as a symlink — tar exits 0, the
    archive holds one entry, and every credential, session and memory is
    absent."""
    target = tmp_path / "big-disk" / "rowan-state"
    target.mkdir(parents=True)
    repo = instance("rowan")
    assert run("register", "rowan", str(repo)).returncode == 0
    home = _home(run, "rowan")
    home.parent.mkdir(parents=True, exist_ok=True)
    home.symlink_to(target)
    (target / ".env").write_text("PLOW_CHAT_TOKEN=x\n")
    (target / "auth.json").write_text("{}\n")

    r = run("backup", "rowan", env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode == 0, r.stderr
    inside = members(next(iter(backup_dir.glob("*.tar.gz"))))
    assert any(m.endswith(".env") for m in inside), \
        f"the symlinked home was stored as a link, not followed: {inside}"
    assert any(m.endswith("auth.json") for m in inside)


def test_the_archive_is_not_readable_by_other_local_accounts(
        run, instance, tmp_path, backup_dir):
    """It holds the dotenv and auth.json for a whole agent. tar honours the
    umask, so under the ordinary 022 -- or this host's 002 -- the archive lands
    0644/0664 and every local account able to traverse the destination can read
    the fleet's credentials straight out of the backups."""
    _, home = registered(run, instance, "rowan")
    (home / ".env").write_text("PLOW_CHAT_TOKEN=x\n")

    assert run("backup", "rowan",
               env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)}).returncode == 0
    archive = next(iter(backup_dir.glob("*.tar.gz")))
    mode = archive.stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"archive is {oct(mode)} — group/other can read credentials"


def test_an_unknown_option_is_refused_rather_than_read_as_a_target(
        run, instance, tmp_path, backup_dir):
    """Target first, then options -- add-skill's shape. The loop that also
    matched a bare word admitted --all anywhere and two targets with
    last-one-wins, permutations nothing documents."""
    registered(run, instance, "rowan")
    r = run("backup", "rowan", "--frobnicate",
            env={"AGENT_MGR_BACKUP_DIR": str(backup_dir)})
    assert r.returncode != 0
    assert "unknown option" in r.stderr

