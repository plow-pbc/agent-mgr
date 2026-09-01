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


def validate_revision(value: str, subject: str, code: ErrorCode) -> str:
    if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise AgentMgrError(code, f"{subject} must be a 40-char SHA, got: {value}")
    return value


def _load_stack(root: Path) -> dict[str, object]:
    raw: object = json.loads((root / "runtime" / "stack.json").read_text())
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "unsupported runtime/stack.json")
    return raw


def image_reference(root: Path = ROOT) -> str:
    images = _load_stack(root).get("images")
    image = images.get("hermes_local") if isinstance(images, dict) else None
    reference = image.get("reference") if isinstance(image, dict) else None
    if not isinstance(reference, str) or "@sha256:" not in reference:
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "hermes_local image is not digest-pinned")
    return reference


def stack(root: Path = ROOT) -> dict[str, Artifact]:
    artifacts = _load_stack(root).get("artifacts")
    if not isinstance(artifacts, dict):
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "unsupported runtime/stack.json")
    result: dict[str, Artifact] = {}
    for name, value in artifacts.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR, "invalid artifact in runtime/stack.json"
            )
        fields = ("repository", "revision", "source", "destination")
        if not all(isinstance(value.get(field), str) for field in fields):
            raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, f"invalid artifact {name}")
        artifact = Artifact(*(value[field] for field in fields))
        validate_revision(
            artifact.revision, f"artifact {name} revision", ErrorCode.INVALID_DESCRIPTOR
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
