from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .errors import AgentMgrError, ErrorCode


def dotenv_read(file: Path, key: str) -> str:
    """The last value bound to `key` in a KEY=VALUE dotenv, or "" if absent.

    Shared by the commands that read one field out of an agent's own .env --
    check-latch's Latch pair, set-home's home channel -- and, for the same
    KEY=VALUE spelling, the two-key credential file. NOT the whole answer for
    any caller whose value may also arrive through Compose: the dotenv is one
    env source among several, and check-latch resolves it against the
    container's own environment for exactly that reason (#165).
    """
    value = ""
    for line in read_regular_text(file).split("\n"):
        found, separator, raw = line.partition("=")
        if separator and found == key:
            value = raw.strip()
    return value


def dotenv_declares(file: Path, key: str) -> bool:
    """Whether `key` is DECLARED in the dotenv -- has an `=`, blank value or
    not -- not merely non-empty. dotenv_read cannot say this: it returns ""
    alike for a blank declaration and an absent key, and the difference is
    exactly what check-latch (#165) needs, since hermes' own
    load_dotenv(override=True) keys off PRESENCE: a declared-but-blank line
    still clobbers a container-supplied value to "".
    """
    for line in read_regular_text(file).split("\n"):
        found, separator, _ = line.partition("=")
        if separator and found == key:
            return True
    return False


def read_regular_text(file: Path) -> str:
    directory = -1
    try:
        directory = os.open(file.parent, os.O_RDONLY | os.O_DIRECTORY)
        fd = os.open(file.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, encoding="utf-8", errors="surrogateescape") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AgentMgrError(ErrorCode.IO_ERROR, f"refusing to read non-regular {file}")
            return handle.read()
    except OSError as exc:
        raise AgentMgrError(ErrorCode.IO_ERROR, f"cannot read {file}: {exc}") from exc
    finally:
        if directory >= 0:
            os.close(directory)


def atomic_write(
    destination: Path, content: bytes, *, stage_in: Path | None = None, mode: int = 0o600
) -> None:
    staged: Path | None = None
    try:
        fd, name = tempfile.mkstemp(
            dir=stage_in or destination.parent, prefix=f".{destination.name}."
        )
        staged = Path(name)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(fd, mode)
            handle.write(content)
            handle.flush()
            os.fsync(fd)
        os.replace(staged, destination)
    finally:
        if staged:
            staged.unlink(missing_ok=True)
