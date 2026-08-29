from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .artifacts import Artifact, fetch, stack
from .errors import AgentMgrError, ErrorCode
from .local import (
    compose,
    confirm_transition,
    environment,
    require_transition_allowed,
    resolve_guard,
    transition,
)
from .models import ResolvedAgent
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent


def migrate_plugin_env(agent: ResolvedAgent, sync: bool = False) -> None:
    dotenv = agent.home / ".env"
    if not dotenv.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {dotenv} -- run 'agent-mgr restore {agent.name}' first"
        )
    args = [str(ROOT / "lib" / "upsert-env"), str(agent.home), "--migrate-legacy"]
    if sync:
        args.append("--sync")
    if subprocess.run(args, check=False).returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"refusing to write {agent.name}'s dotenv -- see above."
        )


def install_plugin(
    agent: ResolvedAgent,
    landed: str = "Legacy dotenv names may have been migrated; this agent's config and skills are untouched.",
) -> None:
    migrate_plugin_env(agent)
    artifact = stack()["plow_chat_plugin"]
    override = os.environ.get("AGENT_MGR_PLUGIN_REF")
    if override:
        if len(override) != 40 or any(c not in "0123456789abcdef" for c in override):
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"the plugin ref must be a 40-char SHA, got: {override}",
            )
        artifact = Artifact(
            artifact.repository, override, artifact.source, artifact.destination, artifact.scope
        )
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


def install_fleet_skills(agent: ResolvedAgent) -> None:
    owned = own_skill_destinations(agent)
    artifacts = stack()
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
            if len(override) != 40 or any(c not in "0123456789abcdef" for c in override):
                raise AgentMgrError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"the fleet-skill ref must be a 40-char SHA, got: {override}",
                )
            artifact = Artifact(
                artifact.repository, override, artifact.source, artifact.destination, artifact.scope
            )
        fetch(agent, "skills", "SKILL.md", artifact, destination=dest)


def replay_skills(agent: ResolvedAgent) -> None:
    manifest = agent.repo / "skills.tsv"
    if not manifest.is_file():
        return
    for line in manifest.read_text().splitlines():
        if not line:
            continue
        repository, revision, destination, source = [*line.split("\t"), ""][:4]
        artifact = Artifact(repository, revision, source, f"skills/{destination}", "local")
        fetch(agent, "skills", "SKILL.md", artifact, destination=destination, source=source)


def reload_if_running(agent: ResolvedAgent, registry: Registry, reason: str) -> None:
    resolve_guard(agent, registry)
    running = compose(agent, ["ps", "--status", "running", "--quiet", "hermes"], capture=True)
    if running.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"could not ask docker whether {agent.name}'s gateway is running"
        )
    if not running.stdout.strip():
        print(f"{agent.name} is not running -- {reason}; it will be read on next start")
        return
    print(f"restarting {agent.name}'s gateway -- {reason}")
    if transition(agent, ["restart", "hermes"]):
        raise AgentMgrError(ErrorCode.IO_ERROR, f"could not restart {agent.name}")


def restore(agent: ResolvedAgent, registry: Registry) -> None:
    resolve_guard(agent, registry)
    confirm_transition(agent)
    require_transition_allowed(agent)
    if not agent.config.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no config at {agent.config} -- an agent repo needs one, or set AGENT_CONFIG in its agent.env",
        )
    if agent.restore_hook and not os.access(agent.restore_hook, os.X_OK):
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"{agent.name} declares a restore hook at {agent.restore_hook}, which is missing or not executable -- nothing was installed",
        )
    agent.home.mkdir(parents=True, exist_ok=True)
    dotenv = agent.home / ".env"
    if not dotenv.exists():
        skeleton = agent.repo / ".env.example"
        if not skeleton.is_file():
            skeleton = ROOT / "templates" / "env.example"
        shutil.copyfile(skeleton, dotenv)
        dotenv.chmod(0o600)
    install_plugin(agent, "The dotenv skeleton IS written; config.yaml and skills are NOT.")
    install_fleet_skills(agent)
    shutil.copyfile(agent.config, agent.home / "config.yaml")
    (agent.home / "config.yaml").chmod(0o600)
    print(f"restored config.yaml to {agent.home}")
    replay_skills(agent)
    if agent.restore_hook:
        hook_env = environment(agent)
        for item in agent.hook_environment:
            key, value = item.split("=", 1)
            hook_env[key] = value
        if subprocess.run(
            [str(agent.restore_hook)], cwd=agent.repo, env=hook_env, check=False
        ).returncode:
            raise AgentMgrError(
                ErrorCode.IO_ERROR,
                f"{agent.name}'s restore hook failed. The config, plugin and pinned skills "
                "ARE installed; the hook's own work is NOT. Fix the cause and re-run "
                f"'agent-mgr restore {agent.name}' before restarting.",
            )
    reload_if_running(agent, registry, "what the restore installed")
