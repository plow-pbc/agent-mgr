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
from .cloud_models import CreateCloudAgentRequest, UpdateCloudAgentChatsRequest
from .commands import (
    activate,
    add_skill,
    chats,
    check_connectors,
    check_latch,
    cron_sync,
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
from .models import JsonValue
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent
JSON_SCHEMA_VERSION = 1
CLOUD_OPERATIONS = frozenset(
    {
        "cloud-create",
        "cloud-list",
        "cloud-get",
        "cloud-update-chats",
        "cloud-delete",
    }
)
NATIVE_JSON_OPERATIONS = (
    frozenset({"ls", "register", "unregister", "new", "resolve"}) | CLOUD_OPERATIONS
)
UNBOUNDED_JSON_OPERATIONS = frozenset({"logs", "compose"})


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


def _json_input(source: str) -> object:
    if source == "-":
        if sys.stdin.isatty():
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                "refusing to wait for cloud JSON on an interactive terminal",
                "pipe a JSON object or pass a file path",
                2,
            )
        text = sys.stdin.read()
    else:
        try:
            text = Path(source).read_text(encoding="utf-8")
        except OSError as error:
            raise AgentMgrError(
                ErrorCode.IO_ERROR, f"could not read cloud request: {error}"
            ) from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"invalid cloud request JSON: {error.msg}"
        ) from error


def _usage(stream: TextIO = sys.stdout) -> None:
    print(
        """usage: agent-mgr [--json] <command> [args]

  ls | register | unregister | new | resolve
  restore | install-plugin | install-skill | add-skill | cron-sync
  activate | sign-in | set-latch | check-latch | chats | set-home
  check-connectors | migrate-plugin-env
  backup-homes | prune-backups
  up | down | restart | logs | agent | compose | resolve-guard
  cloud-create <request.json|->
  cloud-list
  cloud-get <agent-id>
  cloud-update-chats <agent-id> <request.json|->
  cloud-delete <agent-id>""",
        file=stream,
    )


def _run(operation: str, args: list[str], json_output: bool, registry: Registry) -> int:
    if operation == "cloud-create":
        _need(args, 1, "agent-mgr --json cloud-create <request.json|->")
        create_request = CreateCloudAgentRequest.from_json(_json_input(args[0]))
        client = CloudClient(HttpCloudTransport.from_environment(os.environ))
        _emit(operation, {"agent": client.create(create_request).to_json()})
        return 0
    if operation == "cloud-list":
        _need(args, 0, "agent-mgr --json cloud-list")
        client = CloudClient(HttpCloudTransport.from_environment(os.environ))
        resources = client.list()
        _emit(operation, {"agents": [resource.to_json() for resource in resources]})
        return 0
    if operation == "cloud-get":
        _need(args, 1, "agent-mgr --json cloud-get <agent-id>")
        client = CloudClient(HttpCloudTransport.from_environment(os.environ))
        _emit(operation, {"agent": client.get(args[0]).to_json()})
        return 0
    if operation == "cloud-update-chats":
        _need(
            args,
            2,
            "agent-mgr --json cloud-update-chats <agent-id> <request.json|->",
        )
        update_request = UpdateCloudAgentChatsRequest.from_json(_json_input(args[1]))
        client = CloudClient(HttpCloudTransport.from_environment(os.environ))
        _emit(operation, {"agent": client.update_chats(args[0], update_request).to_json()})
        return 0
    if operation == "cloud-delete":
        _need(args, 1, "agent-mgr --json cloud-delete <agent-id>")
        client = CloudClient(HttpCloudTransport.from_environment(os.environ))
        _emit(operation, {"agent": client.delete(args[0]).to_json()})
        return 0
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
            print(f"{'NAME':16} REPO")
            for entry in entries:
                print(f"{entry.name:16} {entry.repo}")
        return 0
    if operation == "register":
        _need(args, 2, "agent-mgr register <name> <dir>")
        repo = registry.add(args[0], Path(args[1]))
        if json_output:
            _emit("register", {"agent": {"name": args[0], "repo": str(repo)}})
        else:
            print(f"registered {args[0]} -> {repo}")
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
        "sign-in",
        "set-latch",
        "check-latch",
        "chats",
        "check-connectors",
    }:
        _need(args, 1, f"agent-mgr {operation} <name>")
        agent = resolve_agent(args[0], registry, ROOT)
        return {
            "cron-sync": cron_sync,
            "activate": activate,
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
        agent = resolve_agent(args[0], registry, ROOT)
        resolve_guard(agent, registry)
        command = {
            "up": ["up", "-d"],
            "down": ["down"],
            "restart": ["restart", "hermes"],
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
    if operation in CLOUD_OPERATIONS and not json_output:
        return _fail(
            operation,
            AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                f"{operation} requires --json",
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
