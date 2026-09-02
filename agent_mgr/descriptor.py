from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .artifacts import image_reference
from .errors import AgentMgrError, ErrorCode
from .files import read_regular_text
from .models import ResolvedAgent
from .registry import Registry

OWNED_KEYS = frozenset(
    {
        "AGENT_NAME",
        "AGENT_DIR",
        "AGENT_HOME",
        "AGENT_CONTAINER",
        "AGENT_PROJECT",
        "AGENT_TZ",
        "AGENT_IMAGE",
        "AGENT_CONFIG",
        "AGENT_LIVE",
        "AGENT_DEPLOY_HOOK",
        "AGENT_PRE_TRANSITION",
        "AGENT_CRON_SPEC",
    }
)
OPTIONAL_PATH_KEYS = frozenset({"AGENT_DEPLOY_HOOK", "AGENT_PRE_TRANSITION", "AGENT_CRON_SPEC"})
# Retired keys die by name: unknown keys pass through as hook environment, so a
# stale descriptor would otherwise resolve fine while the renamed behavior
# (a live agent's guard, its deploy hook) silently stopped applying.
RENAMED_KEYS = {
    "AGENT_CONFIRM_TRANSITIONS": "AGENT_LIVE",
    "AGENT_RESTORE_HOOK": "AGENT_DEPLOY_HOOK",
}
KEY = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class ParsedDescriptor:
    values: dict[str, str]
    hook_environment: tuple[str, ...]


def _value(raw: str, file: Path, line: int, key: str) -> str:
    value = raw.lstrip()
    if value.startswith(('"', "'")):
        quote = value[0]
        end = value.find(quote, 1)
        if end < 0:
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR,
                f"{file}: line {line}: unterminated quote in value for {key}",
            )
        result = value[1:end]
    else:
        result = value.split(" #", 1)[0].rstrip()
    home = str(Path.home())
    return result.replace("${HOME}", home).replace("$HOME", home)


def _assignments(
    text: str, file: Path, wanted: frozenset[str] | None = None
) -> Iterator[tuple[int, str, str]]:
    for number, original in enumerate(text.splitlines(), 1):
        line = original.lstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and len(line) > 6 and line[6].isspace():
            line = line[6:].lstrip()
        if "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.rstrip()
        if wanted is not None and key not in wanted:
            continue
        if not KEY.fullmatch(key):
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR, f"{file}: line {number}: malformed key"
            )
        yield number, key, _value(raw_value, file, number, key)


def parse_descriptor(file: Path) -> ParsedDescriptor:
    try:
        text = file.read_text(errors="surrogateescape")
    except OSError as exc:
        raise AgentMgrError(ErrorCode.IO_ERROR, f"cannot read {file}") from exc
    values: dict[str, str] = {}
    hooks: list[str] = []
    for number, key, value in _assignments(text, file):
        replacement = RENAMED_KEYS.get(key)
        if replacement:
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR,
                f"{file}: line {number}: {key} is now {replacement} -- rename it",
            )
        if key in OWNED_KEYS:
            if not value and key not in OPTIONAL_PATH_KEYS:
                raise AgentMgrError(
                    ErrorCode.INVALID_DESCRIPTOR, f"{file}: line {number}: empty value for {key}"
                )
            values[key] = value
        else:
            hooks.append(f"{key}={value}")
    return ParsedDescriptor(values, tuple(hooks))


def _repo_path(repo: Path, value: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else repo / path


def _read_timezone(file: Path) -> str | None:
    timezone: str | None = None
    for number, _, value in _assignments(read_regular_text(file), file, frozenset({"AGENT_TZ"})):
        timezone = value
        if not timezone:
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR, f"{file}: line {number}: empty value for AGENT_TZ"
            )
    return timezone


def resolve_agent(name: str, registry: Registry, root: Path) -> ResolvedAgent:
    repo = registry.lookup(name)
    if not repo.is_dir():
        raise AgentMgrError(ErrorCode.IO_ERROR, f"{name} points at {repo}, which no longer exists")
    descriptor = repo / "agent.env"
    if not descriptor.is_file():
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR, f"{repo} has no agent.env -- an agent repo needs one"
        )
    parsed = parse_descriptor(descriptor)
    values = parsed.values
    home = Path(values.get("AGENT_HOME", str(Path.home() / f".hermes-{name}"))).absolute()
    timezone = values.get("AGENT_TZ")
    dotenv = home / ".env"
    if dotenv.is_file():
        timezone = _read_timezone(dotenv) or timezone
    reference = image_reference(root)
    config = _repo_path(repo, values.get("AGENT_CONFIG", "config.yaml"))
    assert config is not None
    return ResolvedAgent(
        name=name,
        repo=repo,
        home=home,
        container=values.get("AGENT_CONTAINER", f"hermes-{name}"),
        project=values.get("AGENT_PROJECT", f"hermes-{name}"),
        timezone=timezone or "America/Los_Angeles",
        image=values.get("AGENT_IMAGE", reference),
        config=config,
        live=values.get("AGENT_LIVE", "0") == "1",
        deploy_hook=_repo_path(repo, values.get("AGENT_DEPLOY_HOOK", "")),
        pre_transition_hook=_repo_path(repo, values.get("AGENT_PRE_TRANSITION", "")),
        cron_spec=_repo_path(repo, values.get("AGENT_CRON_SPEC", "")),
        descriptor=descriptor,
        hook_environment=parsed.hook_environment,
        home_declared="AGENT_HOME" in values,
    )
