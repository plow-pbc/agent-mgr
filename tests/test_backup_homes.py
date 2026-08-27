"""What `backup-homes` must not silently get wrong.

Every fact here is measured rather than assumed, and every one of them is
invisible in the output when it breaks — the run exits 0 and the archives still
exist and still look like backups.
"""
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parent.parent / "agent-mgr"


@pytest.fixture(params=["plain", "symlinked"])
def dest(tmp_path, request):
    """Two things at once, so neither needs its own near-identical test.

    The mode: `backup-homes` refuses a destination anyone else can write, and
    the runner's umask is not ours to assume — this host's default of 002 makes
    a plain `mkdir` 0775. Pinning it is the same move as forcing `umask 022` on
    the child, so each test asserts the script's behaviour rather than the
    host's.

    The shape: a destination symlinked onto a bigger disk is supported, the same
    as a home, and the ownership check has to follow it to the target rather
    than refuse the link. Running every test below against both is what pins
    that."""
    d = tmp_path / "dest"
    if request.param == "plain":
        d.mkdir()
        d.chmod(0o700)
    else:
        target = tmp_path / "big" / "store"
        target.mkdir(parents=True)
        target.chmod(0o700)
        d.symlink_to(target)
    return d


def spawn(home_root, dest):
    """The launch contract, in one place. `run()` is this plus a wait."""
    # umask 022 forced on the child: it would otherwise inherit the runner's, and
    # on a host already defaulting to 0077 the mode assertion below would pass
    # with `umask 077` deleted from the script — silently ceasing to guard the
    # credential exposure it exists for.
    # Through the CLI, not the library file: `agent-mgr backup-homes` is the only
    # installed entry point, so every assertion below crosses the dispatch arm —
    # a dropped arm, a lost "$@", or a swallowed exit status fails the suite.
    return subprocess.Popen([str(CLI), "backup-homes", str(dest)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            preexec_fn=lambda: os.umask(0o022),
                            env={"HOME": str(home_root),
                                 "AGENT_MGR_REGISTRY": str(home_root / "registry"),
                                 # The suite poisons PATH with its own docker
                                 # stub and refuses any env not carrying it.
                                 "PATH": os.environ["PATH"]})


def run(home_root, dest):
    p = spawn(home_root, dest)
    out, err = p.communicate()
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def test_a_symlinked_home_is_archived_and_the_bulk_is_not(tmp_path, dest):
    """A home symlinked onto a bigger disk is supported. Archived from the
    PARENT, tar stores that symlink as a symlink and exits 0 with an archive
    holding one entry and no credentials at all. And logs/ is most of the bytes
    and none of the value."""
    root, target = tmp_path / "home", tmp_path / "big" / "state"
    target.mkdir(parents=True), root.mkdir()
    (root / ".hermes-rowan").symlink_to(target)
    (target / ".env").write_text("PLOW_CHAT_TOKEN=x\n")
    (target / "logs").mkdir()
    (target / "logs" / "bulk").write_text("x" * 4096)

    assert run(root, dest).returncode == 0
    inside = set(tarfile.open(next(dest.glob("*/*.tar.gz"))).getnames())
    assert "./.env" in inside, f"the symlink was stored, not followed: {inside}"
    assert not any("logs" in m for m in inside)


def test_neither_the_run_directory_nor_the_archives_are_readable_by_others(tmp_path, dest):
    """They hold .env and auth.json. Both modes come from `umask 077` and both
    fail open without it — archives land 0644, or 0664 on a host with a laxer
    default, which is what happened the first time these were taken by hand. The
    directory matters as much: it is what stands between a predictable archive
    name and anything else able to write the destination. The forced `umask 022`
    in run() removes the runner's own umask as a second source, so this asserts
    the script's guarantee and not the host's."""
    root = tmp_path / "home"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    assert run(root, dest).returncode == 0
    archive = next(dest.glob("*/*.tar.gz"))
    for path in (archive, archive.parent):
        mode = path.stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"{path.name} is {oct(mode)}"


def test_a_run_that_matches_no_home_fails_instead_of_reporting_success(tmp_path, dest):
    """Under the nightly cron the likely cause is a crontab installed under a
    different account, so $HOME is not the operator's. Exiting 0 there means
    every run reports success having written nothing, discovered at restore."""
    root = tmp_path / "empty-home"
    root.mkdir()

    r = run(root, dest)
    assert r.returncode != 0
    assert "no homes matched" in r.stderr

def test_each_run_gets_its_own_directory(tmp_path, dest):
    """The run directory carries a UTC timestamp *and* the pid, so no two runs
    share one and no archive path is ever reused. Asserting the shape catches
    both regressions deterministically; counting directories only catches the
    date-only one, and then only when the two runs land in the same second."""
    root = tmp_path / "home"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    ps = [spawn(root, dest) for _ in range(2)]
    # communicate(), not wait(): spawn() gives the children pipes, and wait()
    # on an undrained pipe deadlocks as soon as a child fills the buffer —
    # tar writes a "file changed as we read it" line per home.
    outs = [p.communicate() for p in ps]
    assert [p.returncode for p in ps] == [0, 0], outs

    names = sorted(d.name for d in dest.iterdir())
    assert len(names) == 2, f"two runs shared a directory: {names}"
    pat = re.compile(r"\d{8}T\d{6}Z-(\d+)")
    pids = [pat.fullmatch(n).group(1) for n in names if pat.fullmatch(n)]
    assert len(pids) == 2, f"run directories lost their timestamp-pid shape: {names}"
    assert pids[0] != pids[1], "both runs used the same pid field"


def test_a_destination_that_does_not_exist_is_refused_and_not_created(tmp_path):
    """An unmounted NAS or a typo'd path. The run must fail rather than build
    the tree locally, report success, and leave the archives on the wrong disk
    — or gone at the next mount."""
    root, dest = tmp_path / "home", tmp_path / "not" / "mounted"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    r = run(root, dest)
    assert r.returncode != 0, r.stdout
    assert not dest.exists(), "it created the destination instead of failing"
    # Its own message, not the loose-permissions one: "must be a directory"
    # invites `mkdir -p`, which is the wrong-disk outcome this test prevents.
    assert "does not exist" in r.stderr, r.stderr


@pytest.mark.parametrize("mode", [0o775, 0o757], ids=["group-writable", "other-writable"])
def test_a_destination_others_can_write_is_refused(tmp_path, mode):
    """The archives hold `.env` and `auth.json`, and anything able to write the
    destination can redirect one — by planting at a predictable name, or by
    replacing the run directory after `mkdir` made it, since write access to a
    directory is permission to unlink and rename its entries. No sequence of
    syscalls inside the run wins that race, so ownership of the destination is
    the precondition and this is where it is enforced."""
    root, dest = tmp_path / "home", tmp_path / "loose-dest"
    (root / ".hermes-rowan").mkdir(parents=True), dest.mkdir()
    (root / ".hermes-rowan" / ".env").write_text("x\n")
    dest.chmod(mode)  # not mkdir(mode=), which the umask masks

    r = run(root, dest)
    assert r.returncode != 0, r.stdout
    assert "nobody else can write" in r.stderr
    assert not any(dest.iterdir()), "it wrote archives anyway"
