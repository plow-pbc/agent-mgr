from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .boot_contract import (
    CREDENTIALS_TARGET,
    CURRENT_HOME,
    credentials_host_path,
    ensure_credentials,
    home_target,
    require_home_target,
)
from .descriptor import resolve_agent
from .errors import AgentMgrError, ErrorCode
from .models import ResolvedAgent
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent
LEAVES_RUNNING = frozenset(
    {
        "logs",
        "ps",
        "config",
        "version",
        "top",
        "port",
        "images",
        "events",
        "ls",
        "exec",
        "run",
        "cp",
        "build",
        "push",
    }
)
NO_IDENTIFICATION = frozenset({"config", "version", "ls", "images", "build", "push", "run", "ps"})
# What a verb does to the current contract's credential promotion, which the
# image's own cont-init runs at container CREATION and never again.
CREATES_CONTAINER = frozenset({"up", "create", "run"})
RESUMES_CONTAINER = frozenset({"start", "restart", "unpause"})
SCRUB = frozenset(
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
        "AGENT_DESCRIPTOR",
        "AGENT_HOME_TARGET",
        "AGENT_CREDENTIALS_HOST",
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_FILE",
        "COMPOSE_ENV_FILE",
        "COMPOSE_ENV_FILES",
        "COMPOSE_PROFILES",
    }
)


def _base_environment(agent: ResolvedAgent) -> dict[str, str]:
    """The env every docker invocation needs, with no boot-contract opinion
    at all -- shared by environment() (which layers the resolved contract on
    top) and build_image() (which deliberately never resolves one)."""
    result = {key: value for key, value in os.environ.items() if key not in SCRUB}
    result.update(agent.environment())
    result["HERMES_UID"] = str(os.getuid())
    result["HERMES_GID"] = str(os.getgid())
    return result


def environment(agent: ResolvedAgent, target: str) -> dict[str, str]:
    """`target` is the caller's already-derived AGENT_HOME_TARGET -- sampled
    once per operation, never re-derived here, so a docker-touching command
    never inspects the contract twice for one decision."""
    result = _base_environment(agent)
    result["AGENT_HOME_TARGET"] = target
    if target == CURRENT_HOME:
        # The path only -- always safe to export. Whether the file at that
        # path is actually current is transition()'s job, right before a
        # container is created.
        result["AGENT_CREDENTIALS_HOST"] = str(credentials_host_path(agent))
    return result


def compose_argv(agent: ResolvedAgent, args: Sequence[str], target: str) -> list[str]:
    files = ["-f", str(ROOT / "templates" / "compose.yml")]
    contract_file = "compose.current.yml" if target == CURRENT_HOME else "compose.legacy.yml"
    files += ["-f", str(ROOT / "templates" / contract_file)]
    override = agent.repo / "compose.override.yml"
    if override.is_file():
        files.extend(["-f", str(override)])
    return [
        "docker",
        "compose",
        "-p",
        agent.project,
        *files,
        "--env-file",
        str(agent.descriptor),
        *args,
    ]


def fetch_is_safe(args: Sequence[str]) -> bool:
    if not args or args[0].startswith("-") or args[0] == "pull":
        return False
    if args[0] == "build":
        return True
    for index, word in enumerate(args):
        if word.startswith("--pull=") and word not in {"--pull=never", "--pull=build"}:
            return False
        if word == "--pull" and (
            index + 1 == len(args) or args[index + 1] not in {"never", "build"}
        ):
            return False
    return True


def require_fetch_safe(args: Sequence[str]) -> None:
    if not fetch_is_safe(args):
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            "refusing a fetch that could replace a built image. Here it is the COMMAND LINE: "
            "'pull' has no accepted form, and '--pull' takes only 'never' or 'build'. "
            "Editing pull_policy will not clear this one. resolve-guard enforces the file "
            "policy, which is the other door. If --pull belongs to a command running "
            "INSIDE the container, wrap it with sh -c so the flag is not on this argv.",
        )


def compose(
    agent: ResolvedAgent, args: Sequence[str], *, capture: bool = False, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    require_fetch_safe(args)
    target = require_home_target(agent)
    verb = args[0] if args else ""
    if target == CURRENT_HOME and verb in RESUMES_CONTAINER:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"refusing 'compose {verb}' for a current-contract agent -- it resumes the "
            "existing container without recreating it, so the boot-time credential "
            f"promotion never reruns. Use 'agent-mgr up {agent.name}' or 'restart', which "
            "force-recreate.",
        )
    if target == CURRENT_HOME and verb in CREATES_CONTAINER:
        # Before docker runs, because the read-only bind source must already BE
        # a file: bind a missing one and Docker leaves a DIRECTORY there that
        # os.replace() can never replace.
        ensure_credentials(agent)
        if verb == "up" and "--force-recreate" not in args:
            args = ["up", "--force-recreate", *args[1:]]
    return subprocess.run(
        compose_argv(agent, args, target),
        env=environment(agent, target),
        text=True,
        input=stdin,
        capture_output=capture,
        check=False,
    )


def build_image(agent: ResolvedAgent) -> int:
    """Materialize a build-based agent's own image from its override alone --
    never the shared template, which needs the boot contract already
    resolved, which is exactly what a not-yet-built image cannot answer yet.
    `build` never starts a container and no Dockerfile reads
    AGENT_HOME_TARGET, so this is the one compose invocation that runs
    before anything can know it. None to inspect afterward if the agent
    declares no override at all -- there is nothing to build."""
    override = agent.repo / "compose.override.yml"
    if not override.is_file():
        return 1
    env = _base_environment(agent)
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            agent.project,
            "-f",
            str(override),
            "--env-file",
            str(agent.descriptor),
            "build",
        ],
        env=env,
        check=False,
    ).returncode


def require_own_home(agent: ResolvedAgent, registry: Registry) -> None:
    own = agent.home.resolve()
    unresolved: tuple[str, str] | None = None
    for entry in registry.entries():
        if entry.name == agent.name:
            continue
        try:
            other = resolve_agent(entry.name, registry, ROOT)
        except AgentMgrError as error:
            print(f"agent-mgr: could not resolve {entry.name} -- {error.message}", file=sys.stderr)
            unresolved = (entry.name, error.message)
            continue
        if other.home.resolve() == own:
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR,
                f"refusing to write to {agent.home} -- {entry.name} is already registered there",
            )
    if unresolved:
        name, _ = unresolved
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR,
            f"refusing to write to {agent.home} -- {name} could not be resolved (reason above), "
            "so this tool cannot prove no one else claims that home. "
            f"Fix the file named above if the agent is still there; 'agent-mgr unregister {name}' only if it is gone.",
        )
    if agent.home.name == f".hermes-{agent.name}":
        return
    if agent.home.name == ".hermes" and agent.home_declared:
        return
    raise AgentMgrError(
        ErrorCode.INVALID_DESCRIPTOR,
        f"refusing to write to {agent.home} -- that is not {agent.name}'s own home",
    )


def resolve_guard(agent: ResolvedAgent, registry: Registry) -> None:
    require_own_home(agent, registry)
    target = require_home_target(agent)
    result = compose(agent, ["config", "--format", "json"], capture=True)
    if result.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"compose could not resolve a config for {agent.name}"
        )
    try:
        config = json.loads(result.stdout)
        if not isinstance(config, dict):
            raise TypeError
        services = config["services"]
        service = services["hermes"]
        project = config.get("name", "-")
        container = service.get("container_name", "-")
        volumes = service.get("volumes", [])
        home = next(
            (item.get("source", "") for item in volumes if item.get("target") == target), "-"
        )
        credential: dict[str, object] = next(
            (item for item in volumes if item.get("target") == CREDENTIALS_TARGET), {}
        )
        agent_id = service.get("environment", {}).get("AGENT_ID", "-")
        image = service.get("image", "-")
        build = bool(service.get("build"))
        pull_policy = service.get("pull_policy")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR,
            f"refusing to act: could not read a Compose config for {agent.name}",
        ) from exc
    checks = [
        (project, agent.project, "project"),
        (container, agent.container, "container"),
        (home, str(agent.home), "home"),
        # The override merges AFTER the template, so it can replace any value
        # the template set -- measured: an override naming AGENT_ID wins.
        # Unchecked, usage would be attributed to a sibling on the same
        # checkout, and the misattribution is invisible: the wrong agent simply
        # looks busier.
        (agent_id, agent.name, "agent id"),
    ]
    if target == CURRENT_HOME:
        # The same merge reaches the credential bind, where it is silent in a
        # way the home check cannot see: the gateway boots clean and posts as
        # whichever sibling the source names. Source and mode as one string --
        # one bind, one invariant, and the refusal names both halves.
        rendered = (
            f"{credential.get('source', '-')} {'ro' if credential.get('read_only') else 'rw'}"
        )
        checks.append((rendered, f"{credentials_host_path(agent)} ro", "credential bind"))
    for got, expected, label in checks:
        if got != expected:
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR,
                f"refusing to act: compose resolved {label} '{got}' but {agent.name} expects '{expected}'",
            )
    if build:
        if pull_policy not in {"never", "build"}:
            shown = "unset (the default, which pulls)" if pull_policy is None else repr(pull_policy)
            raise AgentMgrError(
                ErrorCode.INVALID_DESCRIPTOR,
                f"refusing to act: {agent.name}'s service builds its image but its pull_policy is {shown}, "
                "and only 'never' or 'build' keep Compose from fetching over the host build",
            )
    elif not isinstance(image, str) or "@sha256:" not in image:
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR,
            f"refusing to act: compose resolved image '{image}' for {agent.name}, which is neither a digest nor built here",
        )


def require_container_ours(agent: ResolvedAgent) -> None:
    found = compose(agent, ["ps", "-a", "--quiet", "hermes"], capture=True)
    if found.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"refusing to touch the container under {agent.project} -- docker could not say whether one exists",
        )
    # Only for the raw docker inspect calls' own subprocess environment below
    # -- not for the mount check itself. During a legacy-to-current
    # migration the two can differ, and using the REPLACEMENT image's target
    # to search an existing container's mounts made a perfectly valid old
    # mount read as foreign.
    env = environment(agent, require_home_target(agent))
    for container_id in found.stdout.split():
        # This container's OWN baked contract, not the replacement image's.
        target = home_target(container_id)
        if target is None:
            raise AgentMgrError(
                ErrorCode.IO_ERROR,
                f"refusing to touch the container running as {agent.project} -- docker could not report its baked HERMES_HOME",
            )
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                f'{{{{range .Mounts}}}}{{{{if eq .Destination "{target}"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}',
                container_id,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        mounted = inspected.stdout.strip()
        if inspected.returncode:
            raise AgentMgrError(
                ErrorCode.IO_ERROR,
                f"refusing to touch the container running as {agent.project} -- docker could not say whose home it mounts",
            )
        if mounted and Path(mounted).resolve() == agent.home.resolve():
            continue
        raise AgentMgrError(
            ErrorCode.INVALID_DESCRIPTOR,
            f"refusing to touch the container running as {agent.project} -- it mounts {mounted or '<nothing>'} "
            f"at {target}, not {agent.name}'s home ({agent.home}). The compose project comes from the agent NAME; "
            f"this descriptor may need its own name, or 'agent-mgr unregister {agent.name}'. "
            f"Check ownership first: docker inspect {container_id}",
        )


def confirm_transition(agent: ResolvedAgent) -> None:
    if not agent.live or os.environ.get("AGENT_TRANSITION_ACK") == "1":
        return
    if sys.stdin.isatty():
        print(
            f"{agent.name} is live -- a restart is user-visible, and its person is messaged.\n"
            "Continue? [y/N] ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        if sys.stdin.readline().strip().lower() in {"y", "yes"}:
            os.environ["AGENT_TRANSITION_ACK"] = "1"
            return
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"refused -- not transitioning {agent.name}"
        )
    raise AgentMgrError(
        ErrorCode.INVALID_ARGUMENT,
        f"refusing non-interactively: {agent.name} is live (AGENT_LIVE=1) and a restart messages its person. "
        "Re-run from a terminal to confirm, or set AGENT_TRANSITION_ACK=1 to acknowledge.",
    )


def transition(agent: ResolvedAgent, args: Sequence[str]) -> int:
    confirm_transition(agent)
    require_container_ours(agent)
    require_transition_allowed(agent)
    return compose(agent, args).returncode


def require_transition_allowed(agent: ResolvedAgent) -> None:
    if not agent.pre_transition_hook:
        return
    if not os.access(agent.pre_transition_hook, os.X_OK):
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"{agent.name} declares a pre-transition guard at {agent.pre_transition_hook}, which is missing or not executable",
        )
    hook_env = environment(agent, require_home_target(agent))
    for item in agent.hook_environment:
        key, value = item.split("=", 1)
        hook_env[key] = value
    if subprocess.run(
        [str(agent.pre_transition_hook)], cwd=agent.repo, env=hook_env, check=False
    ).returncode:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name}'s pre-transition guard refused -- not transitioning the container",
        )


def running_container_id(agent: ResolvedAgent) -> str:
    result = compose(agent, ["ps", "--status", "running", "--quiet", "hermes"], capture=True)
    if result.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"could not ask docker whether {agent.name}'s gateway is running"
        )
    ids = result.stdout.split()
    if not ids:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name}'s gateway is not running -- start it first: agent-mgr up {agent.name}",
        )
    return ids[0]


def require_running(agent: ResolvedAgent, registry: Registry) -> str:
    """The running container's id, so callers that go on to exec into it reuse
    this one snapshot rather than asking docker again."""
    resolve_guard(agent, registry)
    container_id = running_container_id(agent)
    require_container_ours(agent)
    return container_id
