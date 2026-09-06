from __future__ import annotations

import contextlib
import os
import shutil
import stat
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
from .files import atomic_write
from .local import (
    build_image,
    compose,
    confirm_transition,
    environment,
    require_container_ours,
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
MANIFEST = ".bundled_manifest"


def retire_staged_copies(agent: ResolvedAgent) -> None:
    """Remove each staged tree the agent's own skills.tsv does not pin, and
    drop its line from the runtime's bundled-skills manifest: the reconcile
    reads a manifest entry with no directory as deleted-by-the-user and never
    re-seeds it (verified on course-qa -- the skill vanished from
    `hermes skills list` until the line went too). Every path is reached one
    O_NOFOLLOW component at a time from the home's own descriptor: the home
    is the gateway's to write, and a parent swapped for a symlink between a
    check and the rmtree is the same hole fetch-tree's parent check closes."""
    owned = own_skill_destinations(agent)
    home_fd = os.open(agent.home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # The manifest first, before anything is removed: a symlinked one is a
        # refusal, and a refusal must leave every tree in place.
        manifest = _read_manifest(agent, home_fd)
        for relative in STAGED_BY_OLDER_DEPLOYS:
            if relative.removeprefix("skills/") in owned:
                continue
            removed = [
                tree
                for tree in (relative, f"{relative}.previous")
                if _rmtree_within(agent, home_fd, tree)
            ]
            for tree in removed:
                print(f"retired {tree} -- the image bundles it now")
            # The skill's manifest line goes with its tree, in the same step:
            # a later refusal must not leave a tree gone and its line present,
            # which the reconcile reads as deleted-by-the-user.
            if removed and manifest is not None and relative.startswith("skills/"):
                name = relative.rsplit("/", 1)[-1]
                manifest = "".join(
                    f"{line}\n" for line in manifest.splitlines() if line.split(":", 1)[0] != name
                )
                _write_manifest(agent, home_fd, manifest)
    finally:
        os.close(home_fd)


def _refusal(agent: ResolvedAgent, relative: str) -> AgentMgrError:
    return AgentMgrError(
        ErrorCode.IO_ERROR,
        f"{agent.home / relative} resolves outside {agent.home} -- refusing to retire through a symlink",
    )


def _open_dir_within(agent: ResolvedAgent, home_fd: int, relative: str) -> int | None:
    """A descriptor on `relative` reached one O_NOFOLLOW component at a time
    from the home's own descriptor, so a gateway swapping a parent for a
    symlink between check and use lands on ELOOP, not on the host. None when
    the path is absent."""
    fd = home_fd
    opened: list[int] = []
    try:
        for part in relative.split("/"):
            try:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise _refusal(agent, relative) from exc
            opened.append(fd)
        return opened.pop()
    finally:
        for stale in opened:
            os.close(stale)


def _rmtree_within(agent: ResolvedAgent, home_fd: int, relative: str) -> bool:
    parent, _, leaf = relative.rpartition("/")
    parent_fd = _open_dir_within(agent, home_fd, parent) if parent else home_fd
    if parent_fd is None:
        return False
    try:
        try:
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        try:
            shutil.rmtree(leaf, dir_fd=parent_fd)
        except NotADirectoryError:
            return False
        except OSError as exc:  # rmtree refuses a symlink leaf; the fd walk refuses a parent
            raise _refusal(agent, relative) from exc
        return True
    finally:
        if parent_fd != home_fd:
            os.close(parent_fd)


def _read_manifest(agent: ResolvedAgent, home_fd: int) -> str | None:
    skills_fd = _open_dir_within(agent, home_fd, "skills")
    if skills_fd is None:
        return None
    try:
        try:
            fd = os.open(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=skills_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _refusal(agent, f"skills/{MANIFEST}") from exc
        with os.fdopen(fd, encoding="utf-8", errors="surrogateescape") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _refusal(agent, f"skills/{MANIFEST}")
            return handle.read()
    finally:
        os.close(skills_fd)


def _write_manifest(agent: ResolvedAgent, home_fd: int, content: str) -> None:
    skills_fd = _open_dir_within(agent, home_fd, "skills")
    if skills_fd is None:
        return
    staged = f".{MANIFEST}.{os.getpid()}"
    try:
        fd = os.open(
            staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=skills_fd
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode())
            handle.flush()
            os.fsync(fd)
        os.replace(staged, MANIFEST, src_dir_fd=skills_fd, dst_dir_fd=skills_fd)
    except OSError as exc:
        raise _refusal(agent, f"skills/{MANIFEST}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(staged, dir_fd=skills_fd)
        os.close(skills_fd)


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


def gateway_running(agent: ResolvedAgent, registry: Registry) -> bool:
    """The reload's fallible half -- the guard and the docker question -- so a
    caller can settle both before a write it must not leave half-applied."""
    resolve_guard(agent, registry)
    running = compose(agent, ["ps", "--status", "running", "--quiet", "hermes"], capture=True)
    if running.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"could not ask docker whether {agent.name}'s gateway is running"
        )
    if running.stdout.strip():
        require_container_ours(agent)
        return True
    return False


def recreate_gateway(agent: ResolvedAgent, running: bool, reason: str) -> None:
    if not running:
        print(f"{agent.name} is not running -- {reason}; it will be read on next start")
        return
    print(f"restarting {agent.name}'s gateway -- {reason}")
    if transition(agent, ["up", "-d", "--force-recreate", "hermes"]):
        raise AgentMgrError(ErrorCode.IO_ERROR, f"could not restart {agent.name}")


def reload_if_running(agent: ResolvedAgent, registry: Registry, reason: str) -> None:
    recreate_gateway(agent, gateway_running(agent, registry), reason)


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
    # Last, after every preflight that can still refuse, and only for an image
    # that bundles what it retires: a legacy base still reads the home copies,
    # and a failed hook or a refusing guard must not leave a running gateway
    # with its plugin gone and no restart behind it.
    running = gateway_running(agent, registry)
    if target == CURRENT_HOME:
        retire_staged_copies(agent)
    recreate_gateway(agent, running, "what the deploy installed")
