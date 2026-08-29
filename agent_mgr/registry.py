from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .errors import AgentMgrError, ErrorCode
from .models import RegistryEntry

VALID_NAME = re.compile(r"^[a-z0-9-]+$")


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_environment(cls) -> Registry:
        configured = os.environ.get("AGENT_MGR_REGISTRY")
        if configured:
            return cls(Path(configured))
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        return cls(base / "agent-mgr" / "agents")

    def entries(self) -> list[RegistryEntry]:
        if not self.path.is_file():
            return []
        rows: list[RegistryEntry] = []
        for line in self.path.read_text().splitlines():
            name, separator, repo = line.partition("\t")
            if separator:
                rows.append(RegistryEntry(name=name, repo=Path(repo)))
        return rows

    def lookup(self, name: str) -> Path:
        for entry in self.entries():
            if entry.name == name:
                return entry.repo
        raise AgentMgrError(
            ErrorCode.AGENT_NOT_FOUND,
            f"{name} is not registered",
            "register the agent repository first",
        )

    def add(self, name: str, repo: Path) -> Path:
        if not VALID_NAME.fullmatch(name):
            raise AgentMgrError(
                ErrorCode.INVALID_NAME,
                f"agent name must be lowercase letters, digits and dashes: {name}",
            )
        if not repo.is_dir():
            raise AgentMgrError(ErrorCode.IO_ERROR, f"no such directory: {repo}")
        canonical = repo.resolve()
        rows = [entry for entry in self.entries() if entry.name != name]
        rows.append(RegistryEntry(name, canonical))
        self._replace(sorted(rows, key=lambda entry: (entry.name, str(entry.repo))))
        return canonical

    def remove(self, name: str) -> None:
        if not self.path.is_file():
            raise AgentMgrError(ErrorCode.IO_ERROR, f"no registry at {self.path}")
        rows = self.entries()
        if not any(entry.name == name for entry in rows):
            raise AgentMgrError(ErrorCode.AGENT_NOT_FOUND, f"{name} is not registered")
        self._replace([entry for entry in rows if entry.name != name])

    def _replace(self, entries: list[RegistryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                for entry in entries:
                    stream.write(f"{entry.name}\t{entry.repo}\n")
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
