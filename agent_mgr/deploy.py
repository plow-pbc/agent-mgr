from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .artifacts import Artifact, fetch, stack, validate_revision
from .descriptor import read_dotenv_values
from .errors import AgentMgrError, ErrorCode
from .files import atomic_write
from .local import (
    compose,
    confirm_transition,
    environment,
    require_own_home,
    require_transition_allowed,
    resolve_guard,
    transition,
)
from .models import ResolvedAgent
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent


def _publish_home_file(source: Path, home: Path, name: str) -> None:
    resolved = home.resolve()
    atomic_write(resolved / name, source.read_bytes(), stage_in=resolved.parent)


CREDENTIAL_KEYS = ("PLOW_API_BASE", "PLOW_AGENT_TOKEN")


def materialize_plow_credentials(agent: ResolvedAgent, registry: Registry) -> Path:
    """Write agent.credentials from this agent's own home dotenv -- beside
    the home, never inside it. The base's root cont-init step promotes it to
    root:root before plow-init reads it, so nothing here runs privileged."""
    require_own_home(agent, registry)
    dotenv = agent.home / ".env"
    if not dotenv.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {dotenv} -- run 'agent-mgr deploy {agent.name}' first"
        )
    values = read_dotenv_values(dotenv, frozenset(CREDENTIAL_KEYS))
    missing = [key for key in CREDENTIAL_KEYS if not values.get(key)]
    if missing:
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR,
            f"{agent.name}'s dotenv ({dotenv}) is missing {' and '.join(missing)} -- "
            "plow-init needs both PLOW_API_BASE and PLOW_AGENT_TOKEN before it can boot. "
            "Nothing was written.",
        )
    body = "".join(f"{key}={values[key]}\n" for key in CREDENTIAL_KEYS)
    atomic_write(agent.credentials, body.encode())
    return agent.credentials


def migrate_plugin_env(agent: ResolvedAgent, sync: bool = False) -> None:
    dotenv = agent.home / ".env"
    if not dotenv.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {dotenv} -- run 'agent-mgr deploy {agent.name}' first"
        )
    args = [str(ROOT / "lib" / "upsert-env"), str(agent.home), "--migrate-legacy"]
    if sync:
        args.append("--sync")
    if subprocess.run(args, check=False).returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"refusing to write {agent.name}'s dotenv -- see above."
        )


def publish_activation_env(agent: ResolvedAgent, remembered_home: str = "") -> None:
    """Atomically pair an activation's fresh legacy token with its durable home."""
    result = subprocess.run(
        [str(ROOT / "lib" / "upsert-env"), str(agent.home), "--publish-activation"],
        input=f"{remembered_home}\n",
        text=True,
        check=False,
    )
    if result.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"refusing to publish {agent.name}'s activation credential -- see above. Nothing was written.",
        )


def install_plugin(
    agent: ResolvedAgent,
    landed: str = "Legacy dotenv names may have been migrated; this agent's config and skills are untouched.",
) -> None:
    migrate_plugin_env(agent)
    artifact = stack()["plow_chat_plugin"]
    override = os.environ.get("AGENT_MGR_PLUGIN_REF")
    if override:
        validate_revision(override, "the plugin ref", ErrorCode.INVALID_ARGUMENT)
        artifact = Artifact(artifact.repository, override, artifact.source, artifact.destination)
    try:
        fetch(agent, "plugins", "plugin.yaml", artifact)
    except AgentMgrError as error:
        raise AgentMgrError(
            error.code,
            f"could not install the Plow Chat plugin from {artifact.repository} at "
            f"{artifact.revision[:7]} -- is 'gh' installed and authenticated "
            f"(gh auth status)? {landed}",
        ) from error


def own_skill_destinations(agent: ResolvedAgent) -> set[str]:
    manifest = agent.repo / "skills.tsv"
    if not manifest.is_file():
        return set()
    return {
        parts[2]
        for line in manifest.read_text().splitlines()
        if len(parts := line.split("\t")) >= 3
    }


def install_fleet_skills(
    agent: ResolvedAgent,
    landed: str = "This agent's config and plugin are untouched.",
) -> None:
    owned = own_skill_destinations(agent)
    artifacts = stack()
    done: list[str] = []
    for key in ("google_workspace_skill", "plow_invite_skill"):
        artifact = artifacts[key]
        dest = artifact.destination.removeprefix("skills/")
        if dest in owned:
            print(
                f"fleet {dest.rsplit('/', 1)[-1]} skill: skipped -- {agent.name}'s own skills.tsv pins {dest}"
            )
            continue
        override = os.environ.get("AGENT_MGR_SKILL_REF")
        if override:
            validate_revision(override, "the fleet-skill ref", ErrorCode.INVALID_ARGUMENT)
            artifact = Artifact(
                artifact.repository, override, artifact.source, artifact.destination
            )
        try:
            fetch(agent, "skills", "SKILL.md", artifact, destination=dest)
        except AgentMgrError as error:
            # Say what landed, like install_plugin and the deploy hook do. This
            # was the one step in deploy that did not, and it is the step most
            # likely to fail on a fresh machine -- where the bare fetch error
            # ("could not install <repo> at <sha>") is also indistinguishable
            # from an unauthenticated `gh`, which refuses even a public repo.
            raise AgentMgrError(
                error.code,
                f"could not install the fleet {dest.rsplit('/', 1)[-1]} skill from "
                f"{artifact.repository} at {artifact.revision[:7]} -- is 'gh' installed "
                f"and authenticated (gh auth status)? "
                + (f"Fleet skills already installed: {', '.join(done)}. " if done else "")
                + landed,
            ) from error
        done.append(dest)


def replay_skills(agent: ResolvedAgent) -> None:
    manifest = agent.repo / "skills.tsv"
    if not manifest.is_file():
        return
    for line in manifest.read_text().splitlines():
        if not line:
            continue
        repository, revision, destination, source = [*line.split("\t"), ""][:4]
        artifact = Artifact(repository, revision, source, f"skills/{destination}")
        fetch(agent, "skills", "SKILL.md", artifact, destination=destination, source=source)


def reload_if_running(agent: ResolvedAgent, registry: Registry, reason: str) -> None:
    resolve_guard(agent, registry)
    if agent.plow_init:
        # Every caller reaches this right after writing something -- a
        # config, a credential -- that might include a fresh
        # PLOW_AGENT_TOKEN, so this is the one place to keep the mounted
        # credential file from serving a token a reload just replaced.
        materialize_plow_credentials(agent, registry)
    running = compose(agent, ["ps", "--status", "running", "--quiet", "hermes"], capture=True)
    if running.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"could not ask docker whether {agent.name}'s gateway is running"
        )
    if not running.stdout.strip():
        print(f"{agent.name} is not running -- {reason}; it will be read on next start")
        return
    print(f"restarting {agent.name}'s gateway -- {reason}")
    if transition(agent, ["up", "-d", "--force-recreate", "hermes"]):
        raise AgentMgrError(ErrorCode.IO_ERROR, f"could not restart {agent.name}")


def deploy(agent: ResolvedAgent, registry: Registry) -> None:
    resolve_guard(agent, registry)
    confirm_transition(agent)
    require_transition_allowed(agent)
    if not agent.config.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no config at {agent.config} -- an agent repo needs one, or set AGENT_CONFIG in its agent.env",
        )
    if agent.deploy_hook and not os.access(agent.deploy_hook, os.X_OK):
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"{agent.name} declares a deploy hook at {agent.deploy_hook}, which is missing or not executable -- nothing was installed",
        )
    agent.home.mkdir(parents=True, exist_ok=True)
    dotenv = agent.home / ".env"
    if not dotenv.exists():
        skeleton = agent.repo / ".env.example"
        if not skeleton.is_file():
            skeleton = ROOT / "templates" / "env.example"
        _publish_home_file(skeleton, agent.home, ".env")
    if agent.plow_init:
        materialize_plow_credentials(agent, registry)
    install_plugin(agent, "The dotenv skeleton IS written; config.yaml and skills are NOT.")
    install_fleet_skills(
        agent, "The dotenv skeleton and the plugin ARE installed; config.yaml was not updated."
    )
    _publish_home_file(agent.config, agent.home, "config.yaml")
    print(f"deployed config.yaml to {agent.home}")
    replay_skills(agent)
    if agent.deploy_hook:
        hook_env = environment(agent)
        for item in agent.hook_environment:
            key, value = item.split("=", 1)
            hook_env[key] = value
        if subprocess.run(
            [str(agent.deploy_hook)], cwd=agent.repo, env=hook_env, check=False
        ).returncode:
            raise AgentMgrError(
                ErrorCode.IO_ERROR,
                f"{agent.name}'s deploy hook failed. The config, plugin and pinned skills "
                "ARE installed; the hook's own work is NOT. Fix the cause and re-run "
                f"'agent-mgr deploy {agent.name}' before restarting.",
            )
    reload_if_running(agent, registry, "what the deploy installed")
