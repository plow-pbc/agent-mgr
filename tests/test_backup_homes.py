"""What `backup-homes` must not silently get wrong.

Two facts, both measured rather than assumed, and both invisible in the output
when they break — the archive still exists and still looks like a backup.
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
    inside = set(tarfile.open(next(dest.glob("*.tar.gz"))).getnames())
    assert "./.env" in inside, f"the symlink was stored, not followed: {inside}"
    assert not any("logs" in m for m in inside)


def test_the_archives_are_not_readable_by_other_accounts(tmp_path):
    """They hold .env and auth.json. `tar` creates the archive honouring the
    umask, so without `umask 077` they land 0644 — or 0664 on a host with a
    laxer default, which is what happened the first time these were taken by
    hand. The forced `umask 022` in run() removes the runner's own umask as a
    second source, so this asserts the script's guarantee and not the host's."""
    root, dest = tmp_path / "home", tmp_path / "dest"
    (root / ".hermes-rowan").mkdir(parents=True), dest.mkdir()
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    assert run(root, dest).returncode == 0
    mode = next(dest.glob("*.tar.gz")).stat().st_mode & 0o777
    assert mode & 0o077 == 0, f"archive is {oct(mode)}"


def test_a_run_that_matches_no_home_fails_instead_of_reporting_success(tmp_path):
    """Under the nightly cron the likely cause is a crontab installed under a
    different account, so $HOME is not the operator's. Exiting 0 there means
    every run reports success having written nothing, discovered at restore."""
    root, dest = tmp_path / "empty-home", tmp_path / "dest"
    root.mkdir(), dest.mkdir()

    r = run(root, dest)
    assert r.returncode != 0
    assert "no homes matched" in r.stderr

def test_each_run_writes_its_own_archive(tmp_path):
    """The name carries a UTC timestamp *and* the pid, so the path is never
    reused. That is what makes `tar` create the file rather than reopen one —
    the only time it honours the umask — and what stops two runs sharing an
    inode. Asserting the shape catches both regressions deterministically; a
    count of archives only catches the date-only one, and then only when the two
    runs happen to land in the same second."""
    root, dest = tmp_path / "home", tmp_path / "dest"
    (root / ".hermes-rowan").mkdir(parents=True), dest.mkdir()
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    ps = [spawn(root, dest) for _ in range(2)]
    assert [p.wait() for p in ps] == [0, 0]

    names = sorted(p.name for p in dest.glob("*.tar.gz"))
    assert len(names) == 2, f"two runs shared a path: {names}"
    pat = re.compile(r"hermes-rowan-\d{8}T\d{6}Z-(\d+)\.tar\.gz")
    pids = [pat.fullmatch(n).group(1) for n in names if pat.fullmatch(n)]
    assert len(pids) == 2, f"archive names lost their timestamp-pid shape: {names}"
    assert pids[0] != pids[1], "both runs used the same pid field"
