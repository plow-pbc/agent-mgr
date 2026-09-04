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
# AGENT_BOOT_CONTRACT selects the compose shape (see templates/compose.plow-init.yml).
# Read only from AGENT_HOME/.env, beside AGENT_TZ, never from this repo
# descriptor -- a repo can be shared across instances (see README's "One repo,
# several people"), and a value read from here would flip every instance
# against it in one edit, with no way for one of them to decline.
BOOT_CONTRACTS = frozenset({"plow-init"})
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


def _read_home_options(file: Path) -> tuple[str | None, str]:
    """One pass over the home dotenv for both keys agent-mgr reads from it.
    AGENT_TZ's blank is refused (indistinguishable from unset);
    AGENT_BOOT_CONTRACT's blank is its safe default, so only a bad value is."""
    timezone: str | None = None
    boot_contract = ""
    wanted = frozenset({"AGENT_TZ", "AGENT_BOOT_CONTRACT"})
    for number, key, value in _assignments(read_regular_text(file), file, wanted):
        if key == "AGENT_TZ":
            timezone = value
            if not timezone:
                raise AgentMgrError(
                    ErrorCode.INVALID_DESCRIPTOR, f"{file}: line {number}: empty value for AGENT_TZ"
                )
        else:
            boot_contract = value
            if boot_contract and boot_contract not in BOOT_CONTRACTS:
                raise AgentMgrError(
                    ErrorCode.INVALID_DESCRIPTOR,
                    f"{file}: line {number}: AGENT_BOOT_CONTRACT must be 'plow-init' or "
                    f"unset, got {boot_contract!r} -- a typo here would silently keep today's shape",
                )
    return timezone, boot_contract


def read_dotenv_values(file: Path, keys: frozenset[str]) -> dict[str, str]:
    """Read exactly `keys` from a home dotenv -- read, never executed, and
    every line not naming one of them skipped before key or value validation
    reaches it. The same parsing `_read_home_options` uses, generalized past two keys."""
    return {key: value for _, key, value in _assignments(read_regular_text(file), file, keys)}


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
    boot_contract = ""
    if dotenv.is_file():
        dotenv_timezone, boot_contract = _read_home_options(dotenv)
        timezone = dotenv_timezone or timezone
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
        boot_contract=boot_contract,
        descriptor=descriptor,
        hook_environment=parsed.hook_environment,
        home_declared="AGENT_HOME" in values,
    )
