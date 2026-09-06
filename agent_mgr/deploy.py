from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .artifacts import Artifact, fetch
from .boot_contract import (
    CURRENT_HOME,
    ensure_image_local,
    image_present_locally,
    require_home_target,
)
from .errors import AgentMgrError, ErrorCode
from .files import atomic_write, read_regular_text
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


def own_skill_destinations(agent: ResolvedAgent) -> set[str]:
    manifest = agent.repo / "skills.tsv"
    if not manifest.is_file():
        return set()
    return {
        parts[2]
        for line in manifest.read_text().splitlines()
        if len(parts := line.split("\t")) >= 3
    }


# What deploys before the image owned them staged into every home. A home
# plugin declaring `name: plow-chat-platform` wins over the image's
# /opt/hermes/plugins/plow_chat, and a home skill directory is "yours was kept"
# to the runtime's bundled-skill reconcile -- so each of these shadows the
# copy the pinned base bundles, and a fix merged upstream never runs (#156).
STAGED_BY_OLDER_DEPLOYS = (
    "plugins/plow-chat-platform",
    "skills/productivity/google-workspace",
    "skills/growth/plow-invite",
)


def retire_staged_copies(agent: ResolvedAgent) -> None:
    """Remove each staged tree the agent's own skills.tsv does not pin, and
    drop its line from the runtime's bundled-skills manifest: the reconcile
    reads a manifest entry with no directory as deleted-by-the-user and never
    re-seeds it (verified on course-qa -- the skill vanished from
    `hermes skills list` until the line went too). Never through a symlink:
    the home is the gateway's to write, and rmtree resolving outside it is
    the same hole fetch-tree's parent check closes."""
    home = agent.home.resolve()
    manifest = agent.home / "skills" / ".bundled_manifest"
    for path in (agent.home / "plugins", agent.home / "skills", manifest):
        if path.is_symlink() or (path.exists() and not path.resolve().is_relative_to(home)):
            raise AgentMgrError(
                ErrorCode.IO_ERROR,
                f"{path} resolves outside {agent.home} -- refusing to retire through a symlink",
            )
    owned = own_skill_destinations(agent)
    for relative in STAGED_BY_OLDER_DEPLOYS:
        if relative.removeprefix("skills/") in owned:
            continue
        for tree in (agent.home / relative, agent.home / f"{relative}.previous"):
            if not tree.is_dir():
                continue
            if tree.is_symlink() or not tree.resolve().is_relative_to(home):
                raise AgentMgrError(
                    ErrorCode.IO_ERROR,
                    f"{tree} resolves outside {agent.home} -- refusing to retire through a symlink",
                )
            shutil.rmtree(tree)
            print(f"retired {tree.relative_to(agent.home)} -- the image bundles it now")
        if relative.startswith("skills/") and manifest.is_file():
            name = relative.rsplit("/", 1)[-1]
            kept = "".join(
                f"{line}\n"
                for line in read_regular_text(manifest).splitlines()
                if not line.startswith(f"{name}:")
            )
            atomic_write(manifest, kept.encode(), stage_in=manifest.parent)


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
    migrate_plugin_env(agent)
    _publish_home_file(agent.config, agent.home, "config.yaml")
    print(f"deployed config.yaml to {agent.home}")
    replay_skills(agent)
    target = require_home_target(agent)
    if agent.deploy_hook:
        hook_env = environment(agent, target)
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
    # Last, and only for an image that bundles what it retires: a legacy base
    # still reads the home copies, and a failed hook above must not leave a
    # running gateway with its plugin gone and no restart behind it.
    if target == CURRENT_HOME:
        retire_staged_copies(agent)
    reload_if_running(agent, registry, "what the deploy installed")
