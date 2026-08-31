from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .backups import backup_homes, prune_backups
from .cloud_client import CloudClient
from .cloud_http import HttpCloudTransport
from .cloud_models import (
    CloudAgentResource,
    CreateCloudAgentRequest,
    UpdateCloudAgentLineRequest,
)
from .commands import (
    activate,
    add_skill,
    chats,
    check_connectors,
    check_latch,
    cron_sync,
    scope_chat_credential,
    set_home,
    set_latch,
    sign_in,
)
from .deploy import (
    install_fleet_skills,
    install_plugin,
    migrate_plugin_env,
    reload_if_running,
    restore,
)
from .descriptor import resolve_agent
from .errors import AgentMgrError, ErrorCode
from .local import (
    LEAVES_RUNNING,
    NO_IDENTIFICATION,
    compose,
    require_container_ours,
    require_fetch_safe,
    require_own_home,
    require_running,
    resolve_guard,
    transition,
)
from .models import JsonValue, RegistryEntry
from .registry import Registry
from .serve import serve

ROOT = Path(__file__).resolve().parent.parent
JSON_SCHEMA_VERSION = 1
CLOUD_OPERATIONS = frozenset(
    {
        "cloud-create",
        "cloud-list",
        "cloud-get",
        "cloud-set-line",
        "cloud-delete",
    }
)
# The two cloud verbs that take a request body on stdin. Only these still
# require --json: the others answer a name the same way their local twins do.
CLOUD_STDIN_OPERATIONS = frozenset({"cloud-create", "cloud-set-line"})
# What a cloud target can and cannot do, stated once. `restart` and `logs` have
# no exe equivalent -- restart would have to delete and re-create, which mints a
# new credential and strands the chat, and exe publishes no log surface at all.
# Refusing by name beats a verb that silently means something else per target.
CLOUD_UNSUPPORTED = {
    "restart": (
        "exe has no restart: it would delete and re-create the agent, minting a new "
        "credential and stranding its chat"
    ),
    "logs": "exe publishes no log surface; read the agent's status with 'agent-mgr cloud-get <name>'",
}
NATIVE_JSON_OPERATIONS = (
    frozenset({"ls", "register", "register-cloud", "unregister", "new", "resolve"})
    | CLOUD_OPERATIONS
)
# `serve` joins them: it runs until interrupted, so the --json capture adapter
# would sit on a child that never exits.
UNBOUNDED_JSON_OPERATIONS = frozenset({"logs", "compose", "serve"})


def _emit(operation: str, result: dict[str, JsonValue]) -> None:
    body: dict[str, JsonValue] = {
        "ok": True,
        "schema_version": JSON_SCHEMA_VERSION,
        "operation": operation,
        "result": result,
    }
    print(json.dumps(body, separators=(",", ":"), sort_keys=True))


def _fail(
    operation: str,
    error: AgentMgrError,
    json_output: bool,
    output: tuple[list[JsonValue], list[JsonValue]] | None = None,
) -> int:
    if json_output:
        details: dict[str, JsonValue] = {
            "code": error.code.value,
            "message": error.message,
            "remediation": error.remediation,
        }
        if output is not None:
            details |= {
                "exit_code": error.exit_code,
                "stdout": output[0],
                "stderr": output[1],
            }
        body: dict[str, JsonValue] = {
            "ok": False,
            "schema_version": JSON_SCHEMA_VERSION,
            "operation": operation,
            "error": details,
        }
        print(json.dumps(body, separators=(",", ":"), sort_keys=True))
    else:
        print(f"agent-mgr: {error.message}", file=sys.stderr)
    return error.exit_code


def _need(args: list[str], count: int, usage: str) -> None:
    if len(args) != count:
        raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, f"usage: {usage}")


def _json_input() -> object:
    if sys.stdin.isatty():
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            "refusing to wait for cloud JSON on an interactive terminal",
            "pipe a JSON object on stdin",
            2,
        )
    text = sys.stdin.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"invalid cloud request JSON: {error.msg}"
        ) from error


def _usage(stream: TextIO = sys.stdout) -> None:
    print(
        """usage: agent-mgr [--json] <command> [args]

  ls | register | register-cloud | unregister | new | resolve
  restore | install-plugin | install-skill | add-skill | cron-sync
  activate | scope-chat-credential | sign-in | set-latch | check-latch | chats | set-home
  check-connectors | migrate-plugin-env
  backup-homes | prune-backups
  up | down | restart | logs | agent | compose | resolve-guard
  serve [host] [port]   Plow's /v1/agents/cloud API, against local containers
  cloud-create | cloud-list | cloud-get | cloud-set-line | cloud-delete

  A cloud agent registered with register-cloud answers the same lifecycle verbs
  as a local one: up, down and chats. restart and logs have no exe equivalent
  and say so rather than meaning something different per target.""",
        file=stream,
    )


def _cloud() -> CloudClient:
    return CloudClient(HttpCloudTransport.from_environment(os.environ))


def _describe(resource: CloudAgentResource) -> str:
    """One line per cloud agent, in the shape `ls` already prints."""
    status = resource.status.value if resource.status else "-"
    joined = ",".join(resource.chat_uids) or "-"
    return f"{resource.agent_id}  {status}  {resource.provider or '-'}  chats={joined}"


def _cloud_result(operation: str, resource: CloudAgentResource, json_output: bool) -> int:
    if json_output:
        _emit(operation, {"agent": resource.to_json()})
    else:
        print(_describe(resource))
    return 0


def _cloud_agent_id(name: str, registry: Registry) -> str:
    """Accept a registered name or a bare agent id, so a cloud verb reads like a
    local one without breaking the id-taking spelling that shipped first."""
    try:
        entry = registry.entry(name)
    except AgentMgrError as error:
        if error.code is not ErrorCode.AGENT_NOT_FOUND:
            raise
        return name
    # A local row does NOT refuse. `agent-mgr serve` publishes an agent's name
    # as its `agent_id`, so against a same-host endpoint the id a cloud verb is
    # given is legitimately the name of a local row -- refusing it rejected
    # every id this repo's own server hands out, before any request was sent.
    # The verb and the id are both explicit; the row is not more authoritative
    # than the caller about which endpoint they meant.
    return entry.location if entry.is_cloud else name


def _delete_cloud_agent(target: str, registry: Registry) -> CloudAgentResource:
    """Delete a tenant and drop the row that named it, in that order.

    Both spellings -- `cloud-delete mary` and `down mary` -- come through here.
    They had separate bodies and only one dropped the row, so `cloud-delete` on
    a registered name deleted the tenant and left a row whose id resolved to
    nothing. The row goes only AFTER the delete succeeds: removing it first
    would strand a live tenant with nothing on this host naming it.
    """
    agent_id = _cloud_agent_id(target, registry)
    client = _cloud()
    try:
        resource = client.delete(agent_id)
    except AgentMgrError as error:
        if error.code in {ErrorCode.INVALID_RESPONSE, ErrorCode.REMOTE_UNREACHABLE}:
            raise AgentMgrError(
                error.code,
                error.message,
                "deletion may have succeeded; run agent-mgr cloud-list before retrying",
            ) from None
        raise
    for entry in registry.entries():
        if entry.is_cloud and entry.location == agent_id:
            registry.remove(entry.name)
    return resource


def _cloud_lifecycle(
    operation: str,
    entry: RegistryEntry,
    args: list[str],
    json_output: bool,
    registry: Registry,
) -> int:
    """The shared lifecycle verbs, against an exe agent.

    `up` is create-or-report rather than create: run twice it must not mint a
    second tenant, and the local `up` is idempotent the same way.
    """
    if operation in CLOUD_UNSUPPORTED:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{operation} is not available for cloud agent {entry.name}: "
            f"{CLOUD_UNSUPPORTED[operation]}",
        )
    client = _cloud()
    if operation == "up":
        # Reports, never creates. A tenant is minted with a credential and a
        # chat grant that `up <name>` carries none of, so creating one here
        # would invent both -- `cloud-create` is where that request belongs.
        return _cloud_result(operation, client.get(entry.location), json_output)
    if operation == "down":
        return _cloud_result(operation, _delete_cloud_agent(entry.name, registry), json_output)
    if operation == "chats":
        resource = client.get(entry.location)
        if json_output:
            _emit(operation, {"chats": list(resource.chat_uids)})
        else:
            for chat in resource.chat_uids:
                print(chat)
        return 0
    raise AgentMgrError(
        ErrorCode.INVALID_ARGUMENT,
        f"{operation} has no cloud equivalent for {entry.name}",
    )


def _run(operation: str, args: list[str], json_output: bool, registry: Registry) -> int:
    if operation == "cloud-create":
        _need(args, 0, "agent-mgr --json cloud-create")
        create_request = CreateCloudAgentRequest.from_json(_json_input())
        client = _cloud()
        try:
            resource = client.create(create_request)
        except AgentMgrError as error:
            if error.code in {ErrorCode.INVALID_RESPONSE, ErrorCode.REMOTE_UNREACHABLE}:
                raise AgentMgrError(
                    error.code,
                    error.message,
                    "creation may have succeeded; run agent-mgr cloud-list before retrying",
                ) from None
            raise
        return _cloud_result(operation, resource, json_output)
    if operation == "cloud-list":
        _need(args, 0, "agent-mgr cloud-list")
        resources = _cloud().list()
        if json_output:
            _emit(operation, {"agents": [resource.to_json() for resource in resources]})
        else:
            for resource in resources:
                print(_describe(resource))
        return 0
    if operation == "cloud-get":
        _need(args, 1, "agent-mgr cloud-get <name|agent-id>")
        return _cloud_result(
            operation, _cloud().get(_cloud_agent_id(args[0], registry)), json_output
        )
    if operation == "cloud-set-line":
        _need(args, 1, "agent-mgr --json cloud-set-line <name|agent-id>")
        update_request = UpdateCloudAgentLineRequest.from_json(_json_input())
        resource = _cloud().update_line(_cloud_agent_id(args[0], registry), update_request)
        return _cloud_result(operation, resource, json_output)
    if operation == "cloud-delete":
        _need(args, 1, "agent-mgr cloud-delete <name|agent-id>")
        return _cloud_result(operation, _delete_cloud_agent(args[0], registry), json_output)
    if operation == "ls":
        _need(args, 0, "agent-mgr ls")
        entries = registry.entries()
        if json_output:
            _emit("ls", {"agents": [entry.to_json() for entry in entries]})
        elif not entries:
            print(
                "no agents registered -- run 'agent-mgr new <name>' or "
                "'agent-mgr register <name> <dir>'"
            )
        else:
            print(f"{'NAME':16} {'TARGET':7} REPO / AGENT ID")
            for entry in entries:
                print(f"{entry.name:16} {entry.target:7} {entry.location}")
        return 0
    if operation == "register":
        _need(args, 2, "agent-mgr register <name> <dir>")
        repo = registry.add(args[0], Path(args[1]))
        if json_output:
            _emit("register", {"agent": {"name": args[0], "repo": str(repo)}})
        else:
            print(f"registered {args[0]} -> {repo}")
        return 0
    if operation == "register-cloud":
        _need(args, 2, "agent-mgr register-cloud <name> <agent-id>")
        agent_id = registry.add_cloud(args[0], args[1])
        if json_output:
            _emit("register-cloud", {"agent": {"name": args[0], "agent_id": agent_id}})
        else:
            print(f"registered {args[0]} -> cloud {agent_id}")
        return 0
    if operation == "unregister":
        _need(args, 1, "agent-mgr unregister <name>")
        registry.remove(args[0])
        _emit("unregister", {"name": args[0]}) if json_output else print(f"unregistered {args[0]}")
        return 0
    if operation == "new":
        if not args or len(args) > 2:
            raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, "usage: agent-mgr new <name> [dir]")
        name = args[0]
        target = Path(args[1]) if len(args) == 2 else Path.cwd() / f"{name}-hermes-agent"
        try:
            existing = registry.lookup(name)
        except AgentMgrError as error:
            if error.code is not ErrorCode.AGENT_NOT_FOUND:
                raise
        else:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"{name} is already registered at {existing} -- pick another name, "
                "or use 'agent-mgr register' to repoint it",
            )
        # Validate before creating the target.
        if not re.fullmatch(r"[a-z0-9-]+", name):
            raise AgentMgrError(
                ErrorCode.INVALID_NAME,
                f"agent name must be lowercase letters, digits and dashes: {name}",
            )
        for filename in ("agent.env", "config.yaml"):
            if (target / filename).exists():
                raise AgentMgrError(
                    ErrorCode.IO_ERROR,
                    f"{target} already has {filename} -- refusing to overwrite it",
                )
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("agent.env", "config.yaml"):
            destination = target / filename
            destination.write_bytes((ROOT / "templates" / filename).read_bytes())
            destination.chmod(0o644)
        registry.add(name, target)
        agent = resolve_agent(name, registry, ROOT)
        if json_output:
            _emit("new", {"agent": agent.to_json()})
        else:
            print(f"scaffolded {name} at {target.resolve()}")
            print(f"  home:     {agent.home} (created by restore)\n")
            print("bring it up:")
            for next_command in (
                "restore",
                "activate",
                "up",
                "cron-sync",
                "sign-in",
                "set-latch",
                "check-latch",
            ):
                print(f"  agent-mgr {next_command} {name}")
        return 0
    if operation == "resolve":
        if not args:
            raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, "which agent? try 'agent-mgr ls'")
        _need(args, 1, "agent-mgr resolve <name>")
        agent = resolve_agent(args[0], registry, ROOT)
        if json_output:
            _emit("resolve", agent.to_json())
        else:
            for key, value in agent.environment().items():
                print(f"{key}={value}")
        return 0
    if operation == "resolve-guard":
        _need(args, 1, "agent-mgr resolve-guard <name>")
        resolve_guard(resolve_agent(args[0], registry, ROOT), registry)
        return 0
    if operation in {"restore", "install-plugin", "install-skill", "migrate-plugin-env"}:
        if not args:
            raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, f"usage: agent-mgr {operation} <name>")
        agent = resolve_agent(args[0], registry, ROOT)
        if operation == "restore":
            _need(args, 1, "agent-mgr restore <name>")
            restore(agent, registry)
        elif operation == "migrate-plugin-env":
            if len(args) > 2 or (len(args) == 2 and args[1] != "--sync"):
                raise AgentMgrError(
                    ErrorCode.INVALID_ARGUMENT,
                    "migrate_plugin_env: unknown mode -- the only mode is --sync",
                )
            require_own_home(agent, registry)
            migrate_plugin_env(agent, len(args) == 2)
        else:
            _need(args, 1, f"agent-mgr {operation} <name>")
            resolve_guard(agent, registry)
            if not agent.home.is_dir():
                raise AgentMgrError(
                    ErrorCode.IO_ERROR,
                    f"no {agent.home} -- run 'agent-mgr restore {agent.name}' first",
                )
            if operation == "install-plugin":
                install_plugin(agent)
                reason = "the plugin just installed"
            else:
                install_fleet_skills(agent)
                reason = "the fleet skills just installed"
            reload_if_running(agent, registry, reason)
        return 0
    if operation in {"backup-homes", "prune-backups"}:
        if not args:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"usage: agent-mgr {operation} <dest>"
                + (" [days]" if operation == "prune-backups" else ""),
            )
        if operation == "backup-homes":
            _need(args, 1, "agent-mgr backup-homes <dest>")
            return backup_homes(args[0])
        if len(args) > 2:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT, "usage: agent-mgr prune-backups <dest> [days]"
            )
        return prune_backups(args[0], args[1] if len(args) == 2 else "14")
    if operation in {
        "cron-sync",
        "activate",
        "scope-chat-credential",
        "sign-in",
        "set-latch",
        "check-latch",
        "chats",
        "check-connectors",
    }:
        _need(args, 1, f"agent-mgr {operation} <name>")
        entry = registry.entry(args[0])
        if entry.is_cloud:
            if operation != "chats":
                raise AgentMgrError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"{operation} runs against a checkout, and {entry.name} is a cloud agent",
                    "exe provisions credentials on first boot; there is nothing to run here",
                )
            return _cloud_lifecycle(operation, entry, args, json_output, registry)
        agent = resolve_agent(args[0], registry, ROOT)
        return {
            "cron-sync": cron_sync,
            "activate": activate,
            "scope-chat-credential": scope_chat_credential,
            "sign-in": sign_in,
            "set-latch": set_latch,
            "check-latch": check_latch,
            "chats": chats,
            "check-connectors": check_connectors,
        }[operation](agent, registry)
    if operation == "add-skill":
        if len(args) < 2:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT, "usage: agent-mgr add-skill <name> <owner/repo>"
            )
        return add_skill(resolve_agent(args[0], registry, ROOT), registry, args[1:])
    if operation == "set-home":
        _need(args, 2, "agent-mgr set-home <name> <cht_...>")
        return set_home(resolve_agent(args[0], registry, ROOT), registry, args[1])
    if operation in {"up", "down", "restart", "logs"}:
        _need(args, 1, f"agent-mgr {operation} <name>")
        entry = registry.entry(args[0])
        if entry.is_cloud:
            return _cloud_lifecycle(operation, entry, args, json_output, registry)
        agent = resolve_agent(args[0], registry, ROOT)
        resolve_guard(agent, registry)
        command = {
            "up": ["up", "-d"],
            "down": ["down"],
            # Compose restart retains the old container definition and misses
            # changes in the shared template.
            "restart": ["up", "-d", "--force-recreate", "hermes"],
            "logs": ["logs", "-f", "--tail", "100"],
        }[operation]
        if operation == "logs":
            require_container_ours(agent)
            return compose(agent, command).returncode
        return transition(agent, command)
    if operation == "agent":
        if len(args) < 2:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT, 'usage: agent-mgr agent <name> "<prompt>"'
            )
        agent = resolve_agent(args[0], registry, ROOT)
        require_running(agent, registry)
        return compose(
            agent,
            [
                "exec",
                "-T",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "hermes",
                "hermes",
                "chat",
                "-q",
                " ".join(args[1:]),
            ],
        ).returncode
    if operation == "compose":
        if len(args) < 2:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                "usage: agent-mgr compose <name> <docker compose args...>",
            )
        agent = resolve_agent(args[0], registry, ROOT)
        command = args[1:]
        if command[0].startswith("-"):
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"refusing 'compose {command[0]}': the subcommand must come first",
            )
        require_fetch_safe(command)
        if command[0] == "run":
            first = command[1] if len(command) > 1 else ""
            if (
                first == "--entrypoint" and (len(command) < 3 or not command[2])
            ) or first == "--entrypoint=":
                raise AgentMgrError(
                    ErrorCode.INVALID_ARGUMENT,
                    "refusing 'compose run': --entrypoint is first but has no value",
                )
            if first == "--entrypoint=" or not (
                first == "--entrypoint" or first.startswith("--entrypoint=")
            ):
                raise AgentMgrError(
                    ErrorCode.INVALID_ARGUMENT,
                    "refusing 'compose run' whose first argument is not --entrypoint: "
                    "without a replaced entrypoint the image's s6 starts a second gateway",
                )
        resolve_guard(agent, registry)
        if command[0] in LEAVES_RUNNING:
            if command[0] not in NO_IDENTIFICATION:
                require_container_ours(agent)
            return compose(agent, command).returncode
        return transition(agent, command)
    if operation == "serve":
        if len(args) > 2:
            raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, "usage: agent-mgr serve [host] [port]")
        host = args[0] if args else "127.0.0.1"
        try:
            port = int(args[1]) if len(args) == 2 else 8765
        except ValueError:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT, f"port must be a number: {args[1]}"
            ) from None
        return serve(registry, ROOT, host, port)
    if operation in {"-h", "--help", "help"}:
        _usage()
        return 0
    raise AgentMgrError(
        ErrorCode.INVALID_ARGUMENT,
        f"unknown command: {operation}\nusage: agent-mgr --help",
        "run 'agent-mgr --help'",
        2,
    )


def main(argv: Sequence[str] | None = None) -> int:
    words = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in words
    if json_output:
        words.remove("--json")
    if not words:
        _usage(sys.stderr)
        return 2
    operation, args = words[0], words[1:]
    # Only the two body-carrying verbs still require --json. Requiring it of the
    # whole cloud namespace made the same lifecycle machine-only on one target
    # and human-usable on the other, which is the parity this CLI is for.
    if operation in CLOUD_STDIN_OPERATIONS and not json_output:
        return _fail(
            operation,
            AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"{operation} reads its request body from stdin and requires --json",
                "rerun with --json",
                2,
            ),
            False,
        )
    if json_output and operation in UNBOUNDED_JSON_OPERATIONS:
        return _fail(
            operation,
            AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"{operation} is unavailable with --json because it can produce unbounded output",
                "run it without --json",
                2,
            ),
            True,
        )
    if json_output and operation not in NATIVE_JSON_OPERATIONS:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "agent-mgr"), operation, *args],
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL if sys.stdin.isatty() else None,
            check=False,
        )
        if completed.returncode == 0:
            stdout: list[JsonValue] = list(completed.stdout.splitlines())
            stderr: list[JsonValue] = list(completed.stderr.splitlines())
            _emit(
                operation,
                {
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )
            return 0
        message = completed.stderr.strip() or completed.stdout.strip() or f"{operation} failed"
        return _fail(
            operation,
            AgentMgrError(
                ErrorCode.OPERATION_FAILED,
                message,
                "inspect the operation output",
                completed.returncode,
            ),
            True,
            (
                list(completed.stdout.splitlines()),
                list(completed.stderr.splitlines()),
            ),
        )
    try:
        return _run(operation, args, json_output, Registry.from_environment())
    except AgentMgrError as error:
        return _fail(operation, error, json_output)


if __name__ == "__main__":
    raise SystemExit(main())
