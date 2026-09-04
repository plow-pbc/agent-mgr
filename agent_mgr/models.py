from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


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
        return "/var/lib/hermes" if self.plow_init else "/opt/data"

    @property
    def credentials(self) -> Path:
        """Where the plow-init credential file lands: beside the home, never
        under it, so the agent's own filesystem tools cannot read it. The
        base's root cont-init step promotes it to root:root from here."""
        return self.home.parent / f".plow-credentials-{self.name}"

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
