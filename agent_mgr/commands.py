from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .artifacts import Artifact, fetch, stack
from .deploy import migrate_plugin_env, reload_if_running
from .errors import AgentMgrError, ErrorCode
from .local import compose, require_own_home, require_running, resolve_guard
from .models import ResolvedAgent
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent


def dotenv_read(file: Path, key: str) -> str:
    value = ""
    for line in file.read_text().split("\n"):
        found, separator, raw = line.partition("=")
        if separator and found == key:
            value = raw.strip()
    return value


def config_declares_latch(file: Path) -> bool:
    inside = False
    for line in file.read_text().splitlines():
        if line == "mcp_servers:":
            inside = True
            continue
        if line and not line[0].isspace():
            inside = False
        if inside and line.strip().startswith("latch:"):
            return True
    return False


def cron_sync(agent: ResolvedAgent, registry: Registry) -> int:
    if not agent.cron_spec:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name} sets no AGENT_CRON_SPEC in agent.env -- it ships no scheduled jobs, so there is nothing to sync",
        )
    if not agent.cron_spec.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"AGENT_CRON_SPEC names {agent.cron_spec}, which does not exist"
        )
    require_running(agent, registry)
    return compose(
        agent,
        [
            "exec",
            "-T",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            "HOME=/opt/data",
            "hermes",
            "/opt/hermes/.venv/bin/python3",
            "-",
            "--spec-json",
            agent.cron_spec.read_text(),
        ],
        stdin=(ROOT / "lib" / "cron-sync.py").read_text(),
    ).returncode


def activate(agent: ResolvedAgent, registry: Registry) -> int:
    require_own_home(agent, registry)
    if not agent.home.is_dir():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {agent.home} -- run 'agent-mgr restore {agent.name}' first"
        )
    artifact = stack()["plow_chat_activation"]
    revision = os.environ.get("AGENT_MGR_ACTIVATE_REF", artifact.revision)
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"the activate ref must be a 40-char SHA, got: {revision}"
        )
    with tempfile.NamedTemporaryFile() as script:
        url = (
            f"https://raw.githubusercontent.com/{artifact.repository}/{revision}/{artifact.source}"
        )
        if subprocess.run(["curl", "-fsSL", url, "-o", script.name], check=False).returncode:
            raise AgentMgrError(
                ErrorCode.IO_ERROR, f"could not fetch activation script at {revision[:7]}"
            )
        result = subprocess.run(["bash", script.name, "--data-dir", str(agent.home)], check=False)
    if result.returncode:
        return result.returncode
    try:
        migrate_plugin_env(agent, True)
    except AgentMgrError:
        print(
            "activation SUCCEEDED under the legacy PLOW_CHAT_* names -- do NOT re-run activate",
            file=sys.stderr,
        )
    try:
        reload_if_running(agent, registry, "the credential just written")
    except AgentMgrError:
        print(
            "activation SUCCEEDED and the credential is written -- do NOT re-run activate",
            file=sys.stderr,
        )
    return 0


def model_provider(file: Path) -> str:
    inside = False
    for line in file.read_text().splitlines():
        if line == "model:":
            inside = True
            continue
        if line and not line[0].isspace():
            inside = False
        if inside and line.split()[:1] == ["provider:"] and len(line.split()) >= 2:
            return line.split()[1]
    raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, f"no model.provider in {file}")


def sign_in(agent: ResolvedAgent, registry: Registry) -> int:
    require_own_home(agent, registry)
    installed = agent.home / "config.yaml"
    if not installed.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {installed} -- run 'agent-mgr restore {agent.name}' first"
        )
    require_running(agent, registry)
    result = compose(
        agent,
        [
            "exec",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "hermes",
            "hermes",
            "auth",
            "add",
            model_provider(installed),
        ],
    )
    if result.returncode == 0:
        reload_if_running(agent, registry, "the credential just written")
    return result.returncode


def add_skill(agent: ResolvedAgent, registry: Registry, args: list[str]) -> int:
    if not args:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            "usage: agent-mgr add-skill <name> <owner/repo> [--ref SHA] [--dest PATH] [--src PATH]",
        )
    repository, rest = args[0], args[1:]
    options = {"--ref": "", "--dest": repository.rsplit("/", 1)[-1], "--src": ""}
    while rest:
        flag = rest.pop(0)
        if flag not in options or not rest:
            raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, f"unknown option: {flag}")
        options[flag] = rest.pop(0)
    resolve_guard(agent, registry)
    revision = options["--ref"]
    if not revision:
        got = subprocess.run(
            ["gh", "api", f"repos/{repository}/commits/HEAD", "--jq", ".sha"],
            text=True,
            capture_output=True,
            check=False,
        )
        if got.returncode:
            raise AgentMgrError(ErrorCode.IO_ERROR, f"could not resolve HEAD for {repository}")
        revision = got.stdout.strip()
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"the skill ref must be a 40-char SHA, got: {revision}"
        )
    artifact = Artifact(
        repository, revision, options["--src"], f"skills/{options['--dest']}", "local"
    )
    fetch(
        agent,
        "skills",
        "SKILL.md",
        artifact,
        destination=options["--dest"],
        source=options["--src"],
    )
    manifest = agent.repo / "skills.tsv"
    rows = (
        [
            line
            for line in manifest.read_text().splitlines()
            if line.split("\t")[2:3] != [options["--dest"]]
        ]
        if manifest.is_file()
        else []
    )
    rows.append(f"{repository}\t{revision}\t{options['--dest']}\t{options['--src']}")
    manifest.write_text("\n".join(sorted(rows)) + "\n")
    reload_if_running(agent, registry, "the skill just installed")
    return 0


def upsert(agent: ResolvedAgent, keys: list[str], values: list[str]) -> None:
    result = subprocess.run(
        [str(ROOT / "lib" / "upsert-env"), str(agent.home), *keys],
        input="\n".join(values) + "\n",
        text=True,
        check=False,
    )
    if result.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"refusing to write {agent.name}'s dotenv -- see above. Nothing was written.",
        )


def set_latch(agent: ResolvedAgent, registry: Registry) -> int:
    require_own_home(agent, registry)
    installed, dotenv = agent.home / "config.yaml", agent.home / ".env"
    if not installed.is_file() or not dotenv.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no {installed if not installed.is_file() else dotenv} -- run 'agent-mgr restore {agent.name}' first",
        )
    if not config_declares_latch(installed):
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name} declares no latch server in {installed} -- nothing would read this pair",
        )
    print("DOMO_DEVICE_UID: ", end="", file=sys.stderr)
    uid = sys.stdin.readline().strip()
    print("DOMO_MCP_TOKEN: ", end="", file=sys.stderr)
    token = sys.stdin.readline().strip()
    upsert(agent, ["DOMO_DEVICE_UID", "DOMO_MCP_TOKEN"], [uid, token])
    print(f"wrote DOMO_DEVICE_UID and DOMO_MCP_TOKEN (...{token[-3:]}) to {dotenv}")
    reload_if_running(agent, registry, "the credential just written")
    print(f"now prove it reaches the Mac: agent-mgr check-latch {agent.name}")
    return 0


def check_latch(agent: ResolvedAgent, registry: Registry) -> int:
    dotenv, installed = agent.home / ".env", agent.home / "config.yaml"
    if not dotenv.is_file() or not installed.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no {dotenv if not dotenv.is_file() else installed} -- run restore first",
        )
    if not config_declares_latch(installed):
        print(f"no latch configured for {agent.name} -- its config declares no latch server")
        return 0
    uid, token = dotenv_read(dotenv, "DOMO_DEVICE_UID"), dotenv_read(dotenv, "DOMO_MCP_TOKEN")
    if not uid or not token:
        missing = "DOMO_DEVICE_UID" if not uid else "DOMO_MCP_TOKEN"
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{missing} is empty in {dotenv} -- mint the pair on the Mac, then: agent-mgr set-latch {agent.name}",
        )
    require_running(agent, registry)
    response = compose(
        agent,
        [
            "exec",
            "-T",
            "hermes",
            "curl",
            "-sS",
            "--max-time",
            "30",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "--config",
            "-",
            "-X",
            "POST",
            f"https://api.plow.co/v1/relay/devices/{uid}/mcp",
            "-H",
            "Content-Type: application/json",
            "-H",
            "Accept: application/json, text/event-stream",
            "-d",
            '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        ],
        capture=True,
        stdin=f'header = "Authorization: Bearer {token}"\n',
    )
    code = response.stdout.strip()
    if code == "200":
        print(f"latch reachable from {agent.name}'s container (HTTP 200)")
        return 0
    if code == "401":
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"DOMO_MCP_TOKEN (...{token[-3:]}) is REVOKED"
        )
    if code == "000":
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no answer from api.plow.co -- the credential was NOT tested: {response.stderr.strip()}",
        )
    raise AgentMgrError(
        ErrorCode.IO_ERROR, f"relay returned HTTP {code or '<none>'}: {response.stderr.strip()}"
    )


def plow_chats(agent: ResolvedAgent, registry: Registry) -> dict[str, object]:
    dotenv = agent.home / ".env"
    token = dotenv_read(dotenv, "PLOW_AGENT_TOKEN") if dotenv.is_file() else ""
    if not token:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"PLOW_AGENT_TOKEN is empty in {dotenv} -- run 'agent-mgr activate {agent.name}' first",
        )
    base = dotenv_read(dotenv, "PLOW_API_BASE") or "https://api.plow.co"
    response = compose(
        agent,
        [
            "exec",
            "-T",
            "hermes",
            "curl",
            "-sS",
            "--max-time",
            "30",
            "--config",
            "-",
            "-w",
            "\n%{http_code}",
            f"{base}/v1/chats",
        ],
        capture=True,
        stdin=f'header = "Authorization: Bearer {token}"\n',
    )
    body, separator, code = response.stdout.rstrip("\n").rpartition("\n")
    if response.returncode or code != "200":
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"GET /v1/chats answered {code or '<none>'} -- the token may be dead",
        )
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "GET /v1/chats returned invalid JSON")
    if not isinstance(parsed.get("data"), list):
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "GET /v1/chats has no data array")
    return parsed


def chats(agent: ResolvedAgent, registry: Registry) -> int:
    require_running(agent, registry)
    home = dotenv_read(agent.home / ".env", "PLOW_HOME_CHANNEL")
    data = plow_chats(agent, registry).get("data", [])
    if not isinstance(data, list):
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "GET /v1/chats has no data array")
    for item in data:
        if not isinstance(item, dict):
            continue
        participants_raw = item.get("participants", [])
        participants = (
            [part for part in participants_raw if isinstance(part, dict)]
            if isinstance(participants_raw, list)
            else []
        )
        line_raw: object = next(
            (part.get("line", {}) for part in participants if part.get("type") == "agent"),
            {},
        )
        line: dict[object, object] = line_raw if isinstance(line_raw, dict) else {}
        members = [
            p.get("display_name") or p.get("provider_key", "?")
            for p in participants
            if p.get("type") == "member"
        ]
        marker = "*" if item.get("uid") == home else " "
        chat_uid = str(item.get("uid", "?"))
        line_uid = str(line.get("uid", "?"))
        provider = str(line.get("provider_key", "?"))
        display = str(item.get("display_name") or ", ".join(members))
        print(f"{marker} {chat_uid:<28} {line_uid:<6} {provider:<13} {display}")
    return 0


def set_home(agent: ResolvedAgent, registry: Registry, uid: str) -> int:
    require_own_home(agent, registry)
    if not re.fullmatch(r"cht_[A-Za-z0-9_-]+", uid):
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"usage: agent-mgr set-home {agent.name} <cht_...>"
        )
    require_running(agent, registry)
    data = plow_chats(agent, registry).get("data", [])
    if not isinstance(data, list):
        raise AgentMgrError(ErrorCode.INVALID_DESCRIPTOR, "GET /v1/chats has no data array")
    if uid not in {item.get("uid") for item in data if isinstance(item, dict)}:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{uid} is not among this token's chats -- run 'agent-mgr chats {agent.name}'",
        )
    upsert(agent, ["PLOW_HOME_CHANNEL", "PLOW_CHAT_CHAT_UID"], [uid, uid])
    print(f"wrote PLOW_HOME_CHANNEL={uid} (and legacy PLOW_CHAT_CHAT_UID) to {agent.home / '.env'}")
    reload_if_running(agent, registry, "the home chat just re-pointed")
    return 0


def check_connectors(agent: ResolvedAgent, registry: Registry) -> int:
    require_running(agent, registry)
    uid = f"{os.getuid()}:{os.getgid()}"
    present = compose(
        agent,
        [
            "exec",
            "-T",
            "--user",
            uid,
            "hermes",
            "test",
            "-f",
            "/opt/data/skills/plow-connectors/plow_connector.py",
        ],
        capture=True,
    )
    if present.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"the plow-connectors skill is not installed in {agent.name} -- run: "
            f"agent-mgr add-skill {agent.name} plow-pbc/plow --dest plow-connectors "
            "--src cloud-agents/hermes/image/seed/skills/productivity/plow-connectors",
        )
    status = 0
    for connector in ("gmail", "slack"):
        result = compose(
            agent,
            [
                "exec",
                "-T",
                "--user",
                uid,
                "hermes",
                "python3",
                "/opt/data/skills/plow-connectors/plow_connector.py",
                connector,
                "status",
            ],
            capture=True,
        )
        if result.returncode:
            print(
                f"{connector}: probe did not run -- {(result.stdout + result.stderr).strip()}",
                file=sys.stderr,
            )
            status = 1
        else:
            print(f"{connector}: {result.stdout.strip()}")
    return status
