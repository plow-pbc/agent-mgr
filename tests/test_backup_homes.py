"""What `backup-homes` must not silently get wrong.

Two facts, both measured rather than assumed, and both invisible in the output
when they break — the archive still exists and still looks like a backup.
"""
import os
import subprocess
import tarfile
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "agent-mgr"


def run(home_root, dest):
    # umask 022 forced on the child: it would otherwise inherit the runner's,
    # and on a host that already defaults to 0077 the mode assertion below
    # passes with `umask 077` deleted from the script — silently ceasing to
    # guard the credential exposure it exists for.
    # Through the CLI, not the library file: `agent-mgr backup-homes` is the only
    # installed entry point, so every assertion below crosses the dispatch arm —
    # a dropped arm, a lost "$@", or a swallowed exit status fails the suite.
    return subprocess.run([str(CLI), "backup-homes", str(dest)],
                          capture_output=True, text=True,
                          preexec_fn=lambda: os.umask(0o022),
                          env={"HOME": str(home_root),
                               "AGENT_MGR_REGISTRY": str(home_root / "registry"),
                               # The suite poisons PATH with its own docker stub
                               # and refuses any env that does not carry it.
                               "PATH": os.environ["PATH"]})


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
    """They hold .env and auth.json. tar honours the umask, so without one they
    land 0644 — or 0664 on a host with a laxer default, which is what happened
    the first time these were taken by hand."""
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


def test_two_overlapping_runs_do_not_publish_each_others_bytes(tmp_path):
    """A manual run overlapping the nightly. With a shared staging path both
    tars write one inode, the first to finish renames it and reports success
    while the other keeps writing into the *published* archive — measured, the
    result passes `gzip -t` and contains the other run's files. A well-formed
    archive whose contents are not what its name says.

    Racy by nature: it can pass without the two overlapping, never fail without
    a real defect."""
    root, dest = tmp_path / "home", tmp_path / "dest"
    (root / ".hermes-rowan").mkdir(parents=True), dest.mkdir()
    (root / ".hermes-rowan" / ".env").write_text("x\n")
    (root / ".hermes-rowan" / "bulk").write_bytes(bytes(8 * 1024 * 1024))

    a = subprocess.Popen([str(CLI), "backup-homes", str(dest)],
                         env={"HOME": str(root), "PATH": os.environ["PATH"],
                              "AGENT_MGR_REGISTRY": str(root / "registry")})
    b = subprocess.Popen([str(CLI), "backup-homes", str(dest)],
                         env={"HOME": str(root), "PATH": os.environ["PATH"],
                              "AGENT_MGR_REGISTRY": str(root / "registry")})
    assert a.wait() == 0 and b.wait() == 0

    archive = next(iter(dest.glob("*.tar.gz")))
    inside = set(tarfile.open(archive).getnames())      # raises on a torn archive
    assert "./.env" in inside and "./bulk" in inside
    assert not list(dest.glob(".hermes-rowan-*")), "a staging file leaked"
