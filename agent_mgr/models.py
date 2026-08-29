from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One registered agent, local or cloud.

    `target` is what the lifecycle verbs dispatch on, so `up`, `down` and the
    rest name a target rather than reading one out of the command they are
    spelled with. A cloud row carries its `exe` agent id in `location`; a local
    row carries its repository path, and `repo` reads it back as one.
    """

    name: str
    location: str
    target: str = "local"

    @property
    def repo(self) -> Path:
        return Path(self.location)

    @property
    def is_cloud(self) -> bool:
        return self.target == "cloud"

    def to_json(self) -> dict[str, JsonValue]:
        # `repo` stays in the payload for every row: it is the documented key,
        # and a cloud row answering `agent_id` alone would break a caller that
        # reads one field for both.
        return {
            "name": self.name,
            "repo": self.location,
            "target": self.target,
            "agent_id": self.location if self.is_cloud else None,
        }


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
    restore_hook: Path | None
    pre_transition_hook: Path | None
    cron_spec: Path | None
    descriptor: Path
    hook_environment: tuple[str, ...]
    home_declared: bool

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
            "restore_hook": str(self.restore_hook) if self.restore_hook else None,
            "pre_transition_hook": (
                str(self.pre_transition_hook) if self.pre_transition_hook else None
            ),
            "cron_spec": str(self.cron_spec) if self.cron_spec else None,
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
            "AGENT_CONFIRM_TRANSITIONS": "",
            "AGENT_RESTORE_HOOK": str(self.restore_hook or ""),
            "AGENT_PRE_TRANSITION": str(self.pre_transition_hook or ""),
            "AGENT_CRON_SPEC": str(self.cron_spec or ""),
            "AGENT_DESCRIPTOR": str(self.descriptor),
        }
