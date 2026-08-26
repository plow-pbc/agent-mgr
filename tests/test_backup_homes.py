"""What `backup-homes` must not silently get wrong.

Two facts, both measured rather than assumed, and both invisible in the output
when they break — the archive still exists and still looks like a backup.
"""
import os
import subprocess
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "backup-homes"


def run(home_root, dest):
    return subprocess.run([str(SCRIPT), str(dest)], capture_output=True, text=True,
                          env={"HOME": str(home_root),
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
