from __future__ import annotations

import os
import re
from pathlib import Path

from .cloud_models import validate_agent_id
from .errors import AgentMgrError, ErrorCode
from .files import atomic_write
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
            name, separator, rest = line.partition("\t")
            if not separator:
                continue
            # Two fields is every row written before targets existed, and it is
            # a local one -- read rather than migrated, so an older agent-mgr
            # sharing this file keeps working.
            location, _, target = rest.partition("\t")
            rows.append(RegistryEntry(name=name, location=location, target=target or "local"))
        return rows

    def entry(self, name: str) -> RegistryEntry:
        for entry in self.entries():
            if entry.name == name:
                return entry
        raise AgentMgrError(
            ErrorCode.AGENT_NOT_FOUND,
            f"{name} is not registered",
            "register the agent repository first",
        )

    def lookup(self, name: str) -> Path:
        entry = self.entry(name)
        if entry.is_cloud:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"{name} is a cloud agent ({entry.location}), not a checkout",
                "cloud agents have no repository on this host",
            )
        return entry.repo

    def add(self, name: str, repo: Path) -> Path:
        if not VALID_NAME.fullmatch(name):
            raise AgentMgrError(
                ErrorCode.INVALID_NAME,
                f"agent name must be lowercase letters, digits and dashes: {name}",
            )
        if not repo.is_dir():
            raise AgentMgrError(ErrorCode.IO_ERROR, f"no such directory: {repo}")
        canonical = repo.resolve()
        self._upsert(RegistryEntry(name, str(canonical), "local"))
        return canonical

    def add_cloud(self, name: str, agent_id: str) -> str:
        """Register an exe agent under a name, so the lifecycle verbs can reach it.

        The same name rules as a local row: the name is the address either way,
        which is the whole point of one verb set over two targets.
        """
        if not VALID_NAME.fullmatch(name):
            raise AgentMgrError(
                ErrorCode.INVALID_NAME,
                f"agent name must be lowercase letters, digits and dashes: {name}",
            )
        # Validated here too, with the transport's own rule: a row holding an
        # id every call would refuse is a row you can write and never use.
        agent_id = validate_agent_id(agent_id.strip())
        self._upsert(RegistryEntry(name, agent_id, "cloud"))
        return agent_id

    def _upsert(self, entry: RegistryEntry) -> None:
        rows = [row for row in self.entries() if row.name != entry.name]
        rows.append(entry)
        self._replace(sorted(rows, key=lambda row: (row.name, row.location)))

    def remove(self, name: str) -> None:
        if not self.path.is_file():
            raise AgentMgrError(ErrorCode.IO_ERROR, f"no registry at {self.path}")
        rows = self.entries()
        if not any(entry.name == name for entry in rows):
            raise AgentMgrError(ErrorCode.AGENT_NOT_FOUND, f"{name} is not registered")
        self._replace([entry for entry in rows if entry.name != name])

    def _replace(self, entries: list[RegistryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A local row keeps its two-field spelling, so this file stays readable
        # by an agent-mgr that predates targets; only a cloud row needs the third.
        content = "".join(
            f"{entry.name}\t{entry.location}\n"
            if entry.target == "local"
            else f"{entry.name}\t{entry.location}\t{entry.target}\n"
            for entry in entries
        )
        atomic_write(self.path, content.encode())
