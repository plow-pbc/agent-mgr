from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

# Every home mount target either boot contract has used. A running
# container was created under whichever was current at the time, so a
# check surviving a descriptor flip (rollback included) needs both.
HOME_MOUNT_TARGETS = ("/opt/data", "/var/lib/hermes")

# Where compose.plow-init.yml mounts the credential file -- fixed by that
# template, not derived per agent, so resolve_guard can check an override
# has not replaced it with a sibling's path or a writable source.
CREDENTIALS_MOUNT_TARGET = "/var/lib/plow/credentials.host"

# The two keys a materialized credential file holds. Shared so a start-time
# guard can compare them against the agent's current home dotenv, not just
# trust that a name-keyed file on disk still belongs to that home.
CREDENTIAL_KEYS = ("PLOW_API_BASE", "PLOW_AGENT_TOKEN")


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    name: str
    repo: Path

    def to_json(self) -> dict[str, JsonValue]:
        return {"name": self.name, "repo": str(self.repo)}


@dataclass(frozen=True, slots=True)
class ResolvedAgent:
    name: str
    repo: Path
    home: Path
    container: str
    project: str
    timezone: str
    image: str
    config: Path
    live: bool
    deploy_hook: Path | None
    pre_transition_hook: Path | None
    cron_spec: Path | None
    boot_contract: str
    descriptor: Path
    hook_environment: tuple[str, ...]
    home_declared: bool

    @property
    def plow_init(self) -> bool:
        return self.boot_contract == "plow-init"

    @property
    def home_mount_target(self) -> str:
        """Where this agent's home lands inside the container -- baked into
        the image as HERMES_HOME under plow-init, agent-mgr's own convention
        otherwise."""
        return HOME_MOUNT_TARGETS[1] if self.plow_init else HOME_MOUNT_TARGETS[0]

    @property
    def credentials(self) -> Path:
        """Fixed under the operator's own home, not beside self.home -- a
        repo-declared AGENT_HOME's checkout is what "beside" would resolve
        to, and a routine `git add .` there would stage a live token."""
        return Path.home() / f".plow-credentials-{self.name}"

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "repo": str(self.repo),
            "home": str(self.home),
            "container": self.container,
            "project": self.project,
            "timezone": self.timezone,
            "image": self.image,
            "config": str(self.config),
            "live": self.live,
            "transition_confirmation_required": self.live,
            "deploy_hook": str(self.deploy_hook) if self.deploy_hook else None,
            "pre_transition_hook": (
                str(self.pre_transition_hook) if self.pre_transition_hook else None
            ),
            "cron_spec": str(self.cron_spec) if self.cron_spec else None,
            "boot_contract": self.boot_contract,
        }

    def environment(self) -> dict[str, str]:
        return {
            "AGENT_NAME": self.name,
            "AGENT_DIR": str(self.repo),
            "AGENT_HOME": str(self.home),
            "AGENT_HOME_TARGET": self.home_mount_target,
            "AGENT_CONTAINER": self.container,
            "AGENT_PROJECT": self.project,
            "AGENT_TZ": self.timezone,
            "AGENT_IMAGE": self.image,
            "AGENT_CONFIG": str(self.config),
            "AGENT_LIVE": "1" if self.live else "0",
            "AGENT_DEPLOY_HOOK": str(self.deploy_hook or ""),
            "AGENT_PRE_TRANSITION": str(self.pre_transition_hook or ""),
            "AGENT_CRON_SPEC": str(self.cron_spec or ""),
            "AGENT_BOOT_CONTRACT": self.boot_contract,
            "AGENT_CREDENTIALS": str(self.credentials),
            "AGENT_DESCRIPTOR": str(self.descriptor),
        }
