from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .artifacts import Artifact, fetch
from .boot_contract import ensure_image_local, image_present_locally, require_home_target
from .errors import AgentMgrError, ErrorCode
from .files import atomic_write
from .local import (
    build_image,
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


def _publish_home_file(source: Path, home: Path, name: str) -> None:
    resolved = home.resolve()
    atomic_write(resolved / name, source.read_bytes(), stage_in=resolved.parent)


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


def _ensure_image_ready(agent: ResolvedAgent) -> None:
    """deploy's own explicit first step: the image must be locally present
    before anything downstream can derive its boot contract or touch its
    container. A digest-pinned image is pulled if absent. A build-based
    agent's own tag is not a registry reference at all -- it cannot be
    pulled, so it is built instead, from its own override alone (never the
    shared template, which needs the contract this not-yet-built image
    cannot answer yet). Once agent.image exists, either way, it is inspected
    directly for its own baked contract -- never substituted for the
    fleet's pinned base, which would silently pick the wrong one for an
    agent whose own build has already moved to the other contract."""
    if image_present_locally(agent.image):
        return
    if "@sha256:" in agent.image:
        ensure_image_local(agent.image)
        return
    if build_image(agent):
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"could not build {agent.image} -- does {agent.repo}/compose.override.yml "
            "declare a build: for it?",
        )


def deploy(agent: ResolvedAgent, registry: Registry) -> None:
    _ensure_image_ready(agent)
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
    _publish_home_file(agent.config, agent.home, "config.yaml")
    print(f"deployed config.yaml to {agent.home}")
    replay_skills(agent)
    if agent.deploy_hook:
        hook_env = environment(agent, require_home_target(agent))
        for item in agent.hook_environment:
            key, value = item.split("=", 1)
            hook_env[key] = value
        if subprocess.run(
            [str(agent.deploy_hook)], cwd=agent.repo, env=hook_env, check=False
        ).returncode:
            raise AgentMgrError(
                ErrorCode.IO_ERROR,
                f"{agent.name}'s deploy hook failed. The config and pinned skills "
                "ARE installed; the hook's own work is NOT. Fix the cause and re-run "
                f"'agent-mgr deploy {agent.name}' before restarting.",
            )
    reload_if_running(agent, registry, "what the deploy installed")
