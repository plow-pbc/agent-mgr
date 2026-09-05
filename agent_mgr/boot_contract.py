from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .errors import AgentMgrError, ErrorCode
from .files import atomic_write, dotenv_read
from .models import ResolvedAgent

# The two boot contracts a Plow Hermes base image can declare, keyed on its own
# baked HERMES_HOME -- never a separate descriptor field, which would be a
# second source of truth for a fact the image already carries.
LEGACY_HOME = "/opt/data"
CURRENT_HOME = "/var/lib/hermes"
KNOWN_HOME_TARGETS = frozenset({LEGACY_HOME, CURRENT_HOME})


def _baked_home_env(target: str) -> str | None:
    """The HERMES_HOME baked into `target`'s Config.Env -- an image reference
    or a container id/name, `docker inspect` takes either.

    None means "could not be read": docker is not on PATH, the daemon is
    unreachable, or (for an image) it is not present locally. Never guessed --
    a wrong guess here is how a gateway starts against an empty home.
    """
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", target],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    try:
        env = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(env, list):
        return None
    for item in env:
        if isinstance(item, str) and item.startswith("HERMES_HOME="):
            return item.removeprefix("HERMES_HOME=")
    return None


def home_target(image: str) -> str | None:
    """`image`'s boot contract, derived from its baked HERMES_HOME.

    None means the image cannot be inspected right now (absent locally, or no
    docker at all) -- a diagnostic caller (resolve, ls) should omit the derived
    field rather than guess. A present image that bakes an unrecognised
    HERMES_HOME is not that case: it fails loudly, because agent-mgr has no
    idea how to boot it.
    """
    baked = _baked_home_env(image)
    if baked is None:
        return None
    if baked not in KNOWN_HOME_TARGETS:
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR,
            f"{image} bakes HERMES_HOME={baked!r}, which agent-mgr does not recognise "
            f"(expected {LEGACY_HOME} or {CURRENT_HOME})",
        )
    return baked


def require_home_target(agent: ResolvedAgent) -> str:
    """Same derivation for a path that has already ensured the image is local
    (deploy) or otherwise needs to touch the container -- absence here is a
    real failure, not a diagnostic gap.

    Always agent.image itself, never a substitute: a build-based agent's own
    tag, once built, may already be a current-contract image -- guessing it
    is "whatever the fleet's pinned base is" would silently mount the wrong
    target for one that already boots differently. deploy's own
    ensure_image_local/build_image is what makes agent.image inspectable in
    the first place, for either shape.
    """
    target = home_target(agent.image)
    if target is None:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"{agent.image} is not present locally -- run 'agent-mgr deploy {agent.name}' first",
        )
    return target


def image_present_locally(image: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, check=False
        )
    except OSError:
        return False
    return result.returncode == 0


def ensure_image_local(image: str) -> None:
    """deploy's own explicit first step: a query, and a pull only if absent.

    Every other container-touching path assumes the image is already local and
    fails loudly instead -- no implicit pull buried in compose argument
    construction.
    """
    if image_present_locally(image):
        return
    if subprocess.run(["docker", "pull", image], check=False).returncode:
        raise AgentMgrError(ErrorCode.IO_ERROR, f"could not pull {image}")


def credentials_host_path(agent: ResolvedAgent) -> Path:
    """Where the current contract's two-key credential file lives on the
    host -- OUTSIDE every agent's own home, deliberately.

    A path under agent.home is one the agent's own (possibly compromised)
    container fully controls, since that whole directory is its mount: it
    could replace this file with a symlink to a SIBLING agent's credential
    file. `Path.is_file()` follows symlinks, and the compose bind is
    read-only, but read-only on the wrong host path still discloses another
    agent's live token -- so the fix is not access mode, it is location.
    Living outside every home means no agent's container has write access to
    this path or its parent at all, symlink or not.
    """
    return Path.home() / f".plow-credentials-{agent.name}"


def ensure_credentials(agent: ResolvedAgent) -> Path:
    """Ensure the current contract's credential file, rather than guard it.

    NOT disposable derived state: after a current-contract container's first
    boot, its own cont-init step promotes this file into
    /var/lib/plow/credentials and, from then on, the gateway's own dotenv load
    truncates PLOW_API_BASE/PLOW_AGENT_TOKEN out of the home .env -- so this
    file becomes the ONLY copy on the host. Deleting it as "redundant" loses
    the agent's credential.

    - home dotenv has both keys -> write or refresh this file from them
    - dotenv lacks them but this file already exists -> leave it alone
    - neither -> fail loudly, naming the agent and what is missing
    """
    destination = credentials_host_path(agent)
    dotenv = agent.home / ".env"
    base = dotenv_read(dotenv, "PLOW_API_BASE") if dotenv.is_file() else ""
    token = dotenv_read(dotenv, "PLOW_AGENT_TOKEN") if dotenv.is_file() else ""
    if base and token:
        atomic_write(destination, f"PLOW_API_BASE={base}\nPLOW_AGENT_TOKEN={token}\n".encode())
        return destination
    if destination.is_file():
        return destination
    raise AgentMgrError(
        ErrorCode.INVALID_ARGUMENT,
        f"{agent.name} has no Plow credential to start from -- {dotenv} carries no "
        f"PLOW_API_BASE/PLOW_AGENT_TOKEN and {destination} does not exist yet. "
        f"Run 'agent-mgr activate {agent.name}' first.",
    )


def read_plow_credentials(agent: ResolvedAgent, target: str) -> tuple[str, str]:
    """(PLOW_API_BASE, PLOW_AGENT_TOKEN), from whichever source is canonical
    under `target` -- the boot contract of the container the caller is about to
    talk to, never the image's, which can already have moved ahead of it.

    The current contract's own gateway truncates both keys out of the home
    dotenv after first boot -- credentials_host_path() is the durable copy
    there, the same file ensure_credentials() writes. The legacy contract has
    no such file; its dotenv stays canonical. Read-only: callers that need to
    WRITE go through ensure_credentials() instead.
    """
    source = credentials_host_path(agent) if target == CURRENT_HOME else agent.home / ".env"
    if not source.is_file():
        return "", ""
    return dotenv_read(source, "PLOW_API_BASE"), dotenv_read(source, "PLOW_AGENT_TOKEN")


def require_running_contract_matches(agent: ResolvedAgent, container_id: str) -> str:
    """cron_sync and check_connectors run via `exec`, which Compose's own
    image-diff never protects -- so compare the running container's actual
    baked HERMES_HOME against the one the agent currently resolves to, and
    refuse on a mismatch. One direct comparison, not a general per-container
    state model. Returns the agreed target, which is what those callers pass
    to exec as HOME."""
    resolved = require_home_target(agent)
    running = _baked_home_env(container_id)
    if running is None:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"docker could not report {agent.name}'s running container's baked HERMES_HOME",
        )
    if running != resolved:
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR,
            f"{agent.name}'s running container was created under HERMES_HOME={running}, but "
            f"its image now resolves to {resolved} -- exec would target the wrong path. "
            f"Recreate it first: agent-mgr restart {agent.name}",
        )
    return resolved
