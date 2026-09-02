from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import termios
from pathlib import Path

from .artifacts import Artifact, fetch, stack, validate_revision
from .cloud_http import HttpCloudTransport
from .deploy import publish_activation_env, reload_if_running
from .errors import AgentMgrError, ErrorCode
from .files import atomic_write, read_regular_text
from .local import compose, require_own_home, require_running, resolve_guard
from .models import ResolvedAgent
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent


def dotenv_read(file: Path, key: str) -> str:
    value = ""
    for line in read_regular_text(file).split("\n"):
        found, separator, raw = line.partition("=")
        if separator and found == key:
            value = raw.strip()
    return value


# The relay endpoint a Latch server is reached at. Matched on the PATH, not the
# host: a self-hosted relay serves the same route from somewhere else, and the
# route is what makes it a relay.
RELAY_MCP_PATH = re.compile(r"/v1/relay/devices/\S*/mcp")


def latch_endpoint(file: Path, dotenv: Path | None = None) -> str | None:
    """The relay URL this config actually declares, with ${VARS} resolved.

    Returns the endpoint rather than a yes/no, because a boolean is what let
    the two halves disagree: the detector accepted any relay URL -- a
    self-hosted one included -- while `check-latch` posted the credential to a
    hardcoded api.plow.co. That sends one relay's bearer to a different host,
    which is a disclosure rather than a failed probe.

    Keyed on the URL rather than the entry's name. The name is not agent-mgr's
    to choose: Hermes prefixes the model's tool names with it --
    `mcp__plow__plow_run_command` -- so shipped agents key the relay `plow` to
    match the base image, and matching the literal `latch:` refused every one
    of them.

    A commented line is not a declaration. Hermes does not load it, so treating
    `# url: ...` as configuration would have `set-latch` write a credential
    nothing reads.
    """
    inside = False
    for line in read_regular_text(file).splitlines():
        if line == "mcp_servers:":
            inside = True
            continue
        if line and not line[0].isspace():
            inside = False
        if not inside:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        key, separator, raw = stripped.partition(":")
        if not separator or key.strip() != "url":
            continue
        value = raw.strip().strip("\"'")
        if not RELAY_MCP_PATH.search(value):
            continue
        if dotenv is not None and dotenv.is_file():
            for name in set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)):
                value = value.replace("${%s}" % name, dotenv_read(dotenv, name))
        return value
    return None


def config_declares_latch(file: Path) -> bool:
    """Whether this agent drives a Mac at all -- `latch_endpoint` is the fact."""
    return latch_endpoint(file) is not None


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
            ErrorCode.IO_ERROR, f"no {agent.home} -- run 'agent-mgr deploy {agent.name}' first"
        )
    dotenv = agent.home / ".env"
    existing_home = (
        dotenv_read(dotenv, "PLOW_HOME_CHANNEL") or dotenv_read(dotenv, "PLOW_CHAT_CHAT_UID")
        if dotenv.is_file()
        else ""
    )
    artifact = stack()["plow_chat_activation"]
    revision = os.environ.get("AGENT_MGR_ACTIVATE_REF", artifact.revision)
    validate_revision(revision, "the activate ref", ErrorCode.INVALID_ARGUMENT)
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
        # The frozen installer writes a fresh legacy pair. Publish its token
        # and the durable pre-bind home in one replacement before narrowing.
        publish_activation_env(agent, existing_home)
        narrow_chat_credential(agent)
    except AgentMgrError as error:
        # The phone bind already succeeded. Report the idempotent follow-up
        # instead of inviting another activation that would mint yet another
        # credential and DM.
        print(
            "activation SUCCEEDED under a broad credential -- do NOT re-run activate; "
            f"run 'agent-mgr scope-chat-credential {agent.name}' after fixing: {error.message}",
            file=sys.stderr,
        )
        return 0
    try:
        reload_if_running(agent, registry, "the credential just written")
    except AgentMgrError:
        print(
            "activation SUCCEEDED and the credential is written -- do NOT re-run activate",
            file=sys.stderr,
        )
    return 0


def narrow_chat_credential(agent: ResolvedAgent) -> int:
    """Convert an activation credential to line reach in place."""
    dotenv = agent.home / ".env"
    if not dotenv.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {dotenv} -- run 'agent-mgr deploy {agent.name}' first"
        )
    home_uid = dotenv_read(dotenv, "PLOW_HOME_CHANNEL")
    token = dotenv_read(dotenv, "PLOW_AGENT_TOKEN")
    if not home_uid or not token:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"incomplete Plow credential in {dotenv} -- activation must write a home and token together",
        )
    base = dotenv_read(dotenv, "PLOW_API_BASE") or "https://api.plow.co"
    transport = HttpCloudTransport.from_environment(
        {"PLOW_API_BASE": base, "PLOW_API_TOKEN": token}
    )
    chat = transport.request("GET", f"/v1/chats/{home_uid}")
    if not isinstance(chat, dict):
        raise AgentMgrError(ErrorCode.INVALID_RESPONSE, "home chat returned invalid JSON")
    participants = chat.get("participants")
    if not isinstance(participants, list):
        raise AgentMgrError(ErrorCode.IO_ERROR, "home chat has no participant roster")
    agents = [
        participant
        for participant in participants
        if isinstance(participant, dict) and participant.get("type") == "agent"
    ]
    self_agents = [
        participant for participant in agents if participant.get("relationship") == "self"
    ]
    current = self_agents[0] if len(self_agents) == 1 else agents[0] if len(agents) == 1 else None
    line = current.get("line") if current is not None else None
    line_uid = line.get("uid") if isinstance(line, dict) else None
    if not isinstance(line_uid, str) or not line_uid:
        raise AgentMgrError(
            ErrorCode.IO_ERROR, "home chat did not identify exactly one current agent line"
        )
    transport.request(
        "PUT",
        "/v1/api-keys/current",
        {
            "name": f"agent-mgr:{agent.name}",
            "scopes": ["chats:use", "llm:chat"],
            "chat_uids": [f"line:{line_uid}"],
        },
    )
    return 0


def scope_chat_credential(agent: ResolvedAgent, registry: Registry) -> int:
    """One-time narrowing for agents activated before line grants existed."""
    require_own_home(agent, registry)
    resolve_guard(agent, registry)
    publish_activation_env(agent)
    result = narrow_chat_credential(agent)
    reload_if_running(agent, registry, "the scoped chat credential just written")
    return result


def model_provider(file: Path) -> str:
    inside = False
    for line in read_regular_text(file).splitlines():
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
            ErrorCode.IO_ERROR, f"no {installed} -- run 'agent-mgr deploy {agent.name}' first"
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
    validate_revision(revision, "the skill ref", ErrorCode.INVALID_ARGUMENT)
    artifact = Artifact(repository, revision, options["--src"], f"skills/{options['--dest']}")
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
    _write_manifest(manifest, "\n".join(sorted(rows)) + "\n")
    reload_if_running(agent, registry, "the skill just installed")
    return 0


def _write_manifest(manifest: Path, content: str) -> None:
    try:
        atomic_write(manifest, content.encode(), mode=0o644)
    except OSError as exc:
        raise AgentMgrError(ErrorCode.IO_ERROR, f"could not publish {manifest}: {exc}") from exc


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


def read_latch_pair() -> tuple[str, str]:
    """The (uid, token) pair from stdin: Latch's JSON blob, or two bare lines."""
    first = sys.stdin.readline()
    if first.lstrip().startswith("{"):
        return latch_pair_from_json(first)
    print("DOMO_MCP_TOKEN: ", end="", file=sys.stderr, flush=True)
    return first.strip(), sys.stdin.readline().strip()


def read_hidden_latch_pair() -> tuple[str, str]:
    """read_latch_pair with terminal echo off -- the JSON paste carries a live
    token, and getpass can only hide a single line. The operator may be
    screen-sharing."""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    hidden = termios.tcgetattr(fd)
    hidden[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, hidden)
    try:
        return read_latch_pair()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        print(file=sys.stderr)


def latch_pair_from_json(first_line: str) -> tuple[str, str]:
    """The (uid, token) pair from the client-config JSON Latch shows once.

    Reads the rest of the blob from stdin: a paste arrives line by line, so it
    accumulates until the text parses. Latch names the server `plow` today, but
    the shape -- one server, a `devices/<uid>/mcp` URL, a Bearer header -- is
    the contract; the name is not."""
    blob = first_line
    while True:
        try:
            data = json.loads(blob)
            break
        except json.JSONDecodeError:
            line = sys.stdin.readline()
            if not line:
                raise AgentMgrError(
                    ErrorCode.INVALID_ARGUMENT,
                    "the paste opened like JSON but is not valid JSON -- "
                    "copy the whole blob from Latch and paste it again",
                ) from None
            blob += line
    try:
        (server,) = data["mcpServers"].values()
        match = re.search(r"/devices/([^/]+)/mcp", server["url"])
        authorization = server["headers"]["Authorization"]
    except (KeyError, ValueError, AttributeError, TypeError):
        match = authorization = None
    if match is None:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            "the JSON has no devices/<uid>/mcp URL under mcpServers -- "
            "is this the blob from Latch's static credential screen?",
        )
    uid = match.group(1)
    token = str(authorization).removeprefix("Bearer ").strip()
    if not str(authorization).startswith("Bearer ") or not token:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            "the JSON has no 'Bearer' Authorization header -- "
            "is this the blob from Latch's static credential screen?",
        )
    return uid, token


def set_latch(agent: ResolvedAgent, registry: Registry) -> int:
    require_own_home(agent, registry)
    installed, dotenv = agent.home / "config.yaml", agent.home / ".env"
    if not installed.is_file() or not dotenv.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no {installed if not installed.is_file() else dotenv} -- run 'agent-mgr deploy {agent.name}' first",
        )
    if not config_declares_latch(installed):
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name} declares no latch server in {installed} -- nothing would read this pair",
        )
    print(
        'Paste the JSON from Latch ("can\'t use OAuth? create a static credential"),'
        " or a bare DOMO_DEVICE_UID (input hidden): ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    uid, token = read_hidden_latch_pair() if sys.stdin.isatty() else read_latch_pair()
    if not uid or not token:
        missing = "DOMO_DEVICE_UID" if not uid else "DOMO_MCP_TOKEN"
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT, f"{missing} was empty -- nothing was written"
        )
    upsert(agent, ["DOMO_DEVICE_UID", "DOMO_MCP_TOKEN"], [uid, token])
    print(f"wrote DOMO_DEVICE_UID and DOMO_MCP_TOKEN to {dotenv}")
    reload_if_running(agent, registry, "the credential just written")
    print(f"now prove it reaches the Mac: agent-mgr check-latch {agent.name}")
    return 0


def check_latch(agent: ResolvedAgent, registry: Registry) -> int:
    dotenv, installed = agent.home / ".env", agent.home / "config.yaml"
    if not dotenv.is_file() or not installed.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no {dotenv if not dotenv.is_file() else installed} -- run deploy first",
        )
    endpoint = latch_endpoint(installed, dotenv)
    if endpoint is None:
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
            # The endpoint the config declares, never a hardcoded host: this
            # request carries the relay's bearer, and posting it anywhere the
            # config did not name hands one relay's credential to another host.
            endpoint,
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
        raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, "DOMO_MCP_TOKEN is REVOKED")
    if code == "000":
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"no answer from {endpoint} -- the credential was NOT tested: {response.stderr.strip()}",
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
            "/opt/data/skills/productivity/plow-connectors/plow_connector.py",
        ],
        capture=True,
    )
    if present.returncode:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"the plow-connectors skill is not installed in {agent.name} -- run: "
            f"agent-mgr add-skill {agent.name} plow-pbc/plow-hermes-agent "
            "--dest productivity/plow-connectors "
            "--src image/seed/skills/productivity/plow-connectors",
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
                "/opt/data/skills/productivity/plow-connectors/plow_connector.py",
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
