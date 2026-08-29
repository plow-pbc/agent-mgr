from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import AgentMgrError, ErrorCode
from .models import ResolvedAgent

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Artifact:
    repository: str
    revision: str
    source: str
    destination: str
    scope: str


def stack() -> dict[str, Artifact]:
    raw = json.loads((ROOT / "runtime" / "stack.json").read_text())
    if raw.get("schema_version") != 1 or not isinstance(raw.get("artifacts"), dict):
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "unsupported runtime/stack.json")
    result: dict[str, Artifact] = {}
    for name, value in raw["artifacts"].items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR, "invalid artifact in runtime/stack.json"
            )
        fields = ("repository", "revision", "source", "destination", "scope")
        if not all(isinstance(value.get(field), str) for field in fields):
            raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, f"invalid artifact {name}")
        artifact = Artifact(*(value[field] for field in fields))
        if len(artifact.revision) != 40 or any(
            c not in "0123456789abcdef" for c in artifact.revision
        ):
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR, f"artifact {name} revision must be a 40-char SHA"
            )
        result[name] = artifact
    return result


def fetch(
    agent: ResolvedAgent,
    kind: str,
    manifest: str,
    artifact: Artifact,
    *,
    destination: str | None = None,
    source: str | None = None,
) -> None:
    dest = destination or artifact.destination.removeprefix(f"{kind}/")
    src = source if source is not None else artifact.source
    child_environment = os.environ.copy()
    child_environment["AGENT_MGR_ROOT"] = str(ROOT)
    completed = subprocess.run(
        [
            str(ROOT / "lib" / "fetch-tree"),
            str(agent.home),
            kind,
            manifest,
            artifact.repository,
            artifact.revision,
            dest,
            src,
        ],
        env=child_environment,
        check=False,
    )
    if completed.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"could not install {artifact.repository} at {artifact.revision[:7]}",
        )
