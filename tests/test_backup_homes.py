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

CLI = Path(__file__).resolve().parent.parent / "agent-mgr"


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


def test_a_symlinked_home_is_archived_and_the_bulk_is_not(tmp_path):
    """A home symlinked onto a bigger disk is supported. Archived from the
    PARENT, tar stores that symlink as a symlink and exits 0 with an archive
    holding one entry and no credentials at all. And logs/ is most of the bytes
    and none of the value."""
    root, target, dest = tmp_path / "home", tmp_path / "big" / "state", tmp_path / "dest"
    target.mkdir(parents=True), root.mkdir(), dest.mkdir()
    (root / ".hermes-rowan").symlink_to(target)
    (target / ".env").write_text("PLOW_CHAT_TOKEN=x\n")
    (target / "logs").mkdir()
    (target / "logs" / "bulk").write_text("x" * 4096)

    assert run(root, dest).returncode == 0
    inside = set(tarfile.open(next(dest.glob("*/*.tar.gz"))).getnames())
    assert "./.env" in inside, f"the symlink was stored, not followed: {inside}"
    assert not any("logs" in m for m in inside)


def test_neither_the_run_directory_nor_the_archives_are_readable_by_others(tmp_path):
    """They hold .env and auth.json. Both modes come from `umask 077` and both
    fail open without it — archives land 0644, or 0664 on a host with a laxer
    default, which is what happened the first time these were taken by hand. The
    directory matters as much: it is what stands between a predictable archive
    name and anything else able to write the destination. The forced `umask 022`
    in run() removes the runner's own umask as a second source, so this asserts
    the script's guarantee and not the host's."""
    root, dest = tmp_path / "home", tmp_path / "dest"
    (root / ".hermes-rowan").mkdir(parents=True), dest.mkdir()
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    assert run(root, dest).returncode == 0
    archive = next(dest.glob("*/*.tar.gz"))
    for path in (archive, archive.parent):
        mode = path.stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"{path.name} is {oct(mode)}"


def test_a_run_that_matches_no_home_fails_instead_of_reporting_success(tmp_path):
    """Under the nightly cron the likely cause is a crontab installed under a
    different account, so $HOME is not the operator's. Exiting 0 there means
    every run reports success having written nothing, discovered at restore."""
    root, dest = tmp_path / "empty-home", tmp_path / "dest"
    root.mkdir(), dest.mkdir()

    r = run(root, dest)
    assert r.returncode != 0
    assert "no homes matched" in r.stderr

def test_each_run_gets_its_own_directory(tmp_path):
    """The run directory carries a UTC timestamp *and* the pid, so no two runs
    share one and no archive path is ever reused. Asserting the shape catches
    both regressions deterministically; counting directories only catches the
    date-only one, and then only when the two runs land in the same second."""
    root, dest = tmp_path / "home", tmp_path / "dest"
    (root / ".hermes-rowan").mkdir(parents=True), dest.mkdir()
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


def test_a_destination_that_does_not_exist_fails_rather_than_being_created(tmp_path):
    """`mkdir`, not `mkdir -p`. An unmounted NAS or a typo'd path is the case:
    with `-p` the run happily builds the whole tree locally, reports success,
    and the archives are on the wrong disk — or gone at the next mount. The
    same one-syscall `mkdir` is what refuses a symlink, a FIFO or a directory
    planted at the run path by anything else able to write the destination."""
    root, dest = tmp_path / "home", tmp_path / "not" / "mounted"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    r = run(root, dest)
    assert r.returncode != 0, r.stdout
    assert not dest.exists(), "it created the destination instead of failing"
