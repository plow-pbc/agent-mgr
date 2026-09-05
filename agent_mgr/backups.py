from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from .errors import AgentMgrError, ErrorCode

BENIGN = re.compile(r"file changed as we read it|file shrank by", re.IGNORECASE)
VOLATILE_REMOVAL = re.compile(
    r"(-wal|-shm|-journal|\.tmp|\.lock|~): file removed before we read it",
    re.IGNORECASE,
)


def _backup_error(message: str) -> AgentMgrError:
    return AgentMgrError(ErrorCode.IO_ERROR, f"backup-homes: {message}")


def _runs_directory(destination: Path) -> Path:
    runs = destination / "backup-homes"
    marker = runs / ".written-by-backup-homes"
    if not runs.exists() and not runs.is_symlink():
        staged = destination / f".backup-homes.{os.getpid()}"
        try:
            staged.mkdir(mode=0o700)
            (staged / ".written-by-backup-homes").touch(mode=0o600)
            with suppress(OSError):
                staged.rename(runs)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
            nested = runs / staged.name
            if nested.exists():
                shutil.rmtree(nested)
    if runs.is_symlink():
        raise _backup_error(
            f"{runs} is a symlink -- replace it with a real directory; no marker will make it usable"
        )
    if not marker.is_file():
        raise _backup_error(
            f"{runs} carries no marker, so this will not write into it. "
            f"If an earlier version made it: touch {marker}. Otherwise move it aside"
        )
    return runs


def _archive(archive: Path, source: Path, members: list[str], subject: str) -> bool:
    """tar `members` out of `source`. False when nothing worth keeping came of
    it, judged on tar's diagnostic rather than its status: status 1 covers both
    a file rewritten under us and one tar could not read at all, and only the
    second loses something.
    """
    result = subprocess.run(
        [
            "tar",
            "--exclude=./logs",
            "--exclude=./cache",
            "--exclude=./lazy-packages",
            "-C",
            str(source),
            "-czf",
            str(archive),
            *members,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    tolerated = bool(result.stderr) and all(
        BENIGN.search(line) or VOLATILE_REMOVAL.search(line) for line in result.stderr.splitlines()
    )
    if result.returncode and not (result.returncode == 1 and tolerated):
        archive.unlink(missing_ok=True)
        print(
            f"backup-homes: tar failed on {subject} (status {result.returncode}) -- no archive kept",
            file=sys.stderr,
        )
        return False
    return True


def backup_homes(destination_name: str) -> int:
    destination = Path(destination_name)
    if not destination.exists():
        raise _backup_error(
            f"{destination} does not exist. To create it: mkdir -p {destination} && "
            f"chmod 700 {destination} -- but if it is a mount point, or a link to one, mount it instead"
        )
    resolved = destination.resolve()
    info = resolved.stat()
    if (
        not resolved.is_dir()
        or info.st_uid != os.getuid()
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise _backup_error(
            f"{destination} must be a directory you own that nobody else can write -- try: chmod 700 {destination}"
        )
    previous_umask = os.umask(0o077)
    try:
        runs = _runs_directory(resolved)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run = runs / f"{stamp}-{os.getpid()}"
        run.mkdir(mode=0o700)
        home_root = Path.home()
        homes = [home_root / ".hermes"] if (home_root / ".hermes").is_dir() else []
        homes.extend(path for path in sorted(home_root.glob(".hermes-*")) if path.is_dir())
        if not homes:
            raise _backup_error(f"no homes matched {home_root}/.hermes*")
        # Outside every home, deliberately (boot_contract.credentials_host_path),
        # so no home's archive reaches it -- and after a current-contract first
        # boot it is the only host-side copy of that agent's Plow token.
        credentials = sorted(p.name for p in home_root.glob(".plow-credentials-*") if p.is_file())
        failed = bool(credentials) and not _archive(
            run / "plow-credentials.tar.gz", home_root, credentials, "the Plow credentials"
        )
        for home in homes:
            if not _archive(run / f"{home.name.removeprefix('.')}.tar.gz", home, ["."], str(home)):
                failed = True
        if failed:
            raise _backup_error("one or more homes or credentials were not archived")
        return 0
    finally:
        os.umask(previous_umask)


def prune_backups(destination_name: str, days_name: str = "14") -> int:
    if not days_name.isdigit():
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"prune-backups: days must be a whole number, not '{days_name}'",
        )
    days = int(days_name)
    if days < 1:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"prune-backups: days must be at least 1, not '{days_name}'"
        )
    runs = Path(destination_name) / "backup-homes"
    if not runs.is_dir():
        raise _backup_error(
            f"no runs at {runs} -- if {destination_name} is a mount point, it is not mounted"
        )
    if runs.is_symlink():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"prune-backups: {runs} is a symlink -- replace it with a real directory",
        )
    marker = runs / ".written-by-backup-homes"
    if not marker.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"prune-backups: {runs} carries no marker -- refusing to delete anything in it. "
            f"If an earlier version of backup-homes made it: touch {marker}",
        )
    threshold = time.time() - days * 86400
    for child in runs.iterdir():
        if child.is_dir() and not child.is_symlink() and child.stat().st_mtime <= threshold:
            shutil.rmtree(child)
    return 0
