from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
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


def config_declares_latch(file: Path) -> bool:
    inside = False
    for line in read_regular_text(file).splitlines():
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
            ErrorCode.IO_ERROR, f"no {dotenv} -- run 'agent-mgr restore {agent.name}' first"
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


class AmbiguousHome(AgentMgrError):
    """The line carries several 1:1 chats, so `set-home` is the recovery.

    A distinct type rather than a substring of the message: dispatching on the
    words meant an API failure whose text happened to differ was read as an
    ambiguity, and an outage sent the operator to `set-home` -- which cannot
    fix an outage. What the recovery is depends on WHY discovery failed, so the
    why has to be typed.
    """


def provision(agent: ResolvedAgent, registry: Registry, line_uid: str) -> int:
    """Mint this agent's credential server-side and write it -- no text, no code.

    This is the whole difference between standing up a local agent and a cloud
    one. `activate` cannot avoid the human: `POST /v1/auth/activate` carries no
    credential, so the account binding is decided by WHICH PHONE texts the code
    back. Here the caller is already authenticated as the owner, so Plow can
    mint against that account directly -- exactly what cloud provisioning does.

    The grant names a LINE rather than a chat list, because an agent's identity
    is its number: a frozen list cannot cover the threads that number receives
    tomorrow, and a group born on it would be invisible until a re-mint.
    """
    require_own_home(agent, registry)
    dotenv = agent.home / ".env"
    if not dotenv.is_file():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {dotenv} -- run 'agent-mgr restore {agent.name}' first"
        )
    if dotenv_read(dotenv, "PLOW_AGENT_TOKEN"):
        # Refused rather than overwritten: a second mint strands the credential
        # the gateway is holding, and the running agent goes deaf on a line it
        # still believes it serves.
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name} already holds a Plow credential",
            "re-minting strands the live one; unregister and restore a fresh home to replace it",
        )
    transport = HttpCloudTransport.from_environment(os.environ)
    minted = transport.request(
        "POST", "/v1/relay/agents", {"name": f"agent-mgr:{agent.name}", "line_uid": line_uid}
    )
    if not isinstance(minted, dict):
        raise AgentMgrError(ErrorCode.INVALID_RESPONSE, "relay mint returned invalid JSON")
    token = minted.get("token")
    if not isinstance(token, str) or not token:
        raise AgentMgrError(ErrorCode.INVALID_RESPONSE, "relay mint returned no token")
    # The token lands BEFORE the home is resolved, and that order is the whole
    # point: the mint is one-time, so anything that raises between receiving it
    # and writing it destroys a credential nobody can get back. Home discovery
    # is exactly such a step -- it refuses an ambiguous line -- and the recovery
    # it names (`set-home`) needs this token persisted and a gateway running to
    # be possible at all. Write first, then decide the home.
    upsert(
        agent,
        ["PLOW_AGENT_TOKEN", "PLOW_API_BASE", "PLOW_CHAT_TOKEN", "PLOW_CHAT_BASE_URL"],
        [token, transport.base_url, token, transport.base_url],
    )
    try:
        home = _home_chat_on_line(transport.base_url, token, line_uid)
    except AgentMgrError as error:
        # Two different failures, and pointing both at `set-home` was wrong:
        # it validates the uid against the SAME listing, so on an empty grant
        # every uid it could be given is rejected -- a recovery that cannot
        # succeed, offered to an operator holding a spent credential.
        #
        # Ambiguity is the set-home case: the chats exist, this tool just may
        # not choose between them. An empty grant is not; nothing is reachable
        # yet, and the fix is on the line, after which set-home works.
        recovery = (
            f"agent-mgr set-home {agent.name} <cht_...>"
            if isinstance(error, AmbiguousHome)
            else f"give {line_uid} a chat this account owns, then "
            f"'agent-mgr set-home {agent.name} <cht_...>'"
        )
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name}'s credential IS written -- do not re-run provision, the mint is "
            f"spent. Its home is not set: {error.message}. Recovery: {recovery}.",
            recovery,
        ) from None
    upsert(
        agent,
        ["PLOW_HOME_CHANNEL", "PLOW_CHAT_CHAT_UID"],
        [home, home],
    )
    # The mint is one-time and the credential is now on disk, so a failed
    # reload must not read as a failed provision: the retry would find the
    # token, refuse as "already holds a credential", and send the operator to
    # unregister a home whose only problem is a container that did not restart.
    try:
        reload_if_running(agent, registry, "the credential just minted")
    except AgentMgrError as error:
        # In the message, not only the remediation: plain-text output prints the
        # message alone, and "do not re-run this" is exactly the sentence an
        # operator must see before they try again on a spent mint.
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"{agent.name}'s credential IS written -- do not re-run provision, the mint is "
            f"spent. The gateway did not reload ({error.message}); "
            f"restart it with 'agent-mgr restart {agent.name}'.",
            f"agent-mgr restart {agent.name}",
        ) from None
    return 0


def _home_chat_on_line(base: str, token: str, line_uid: str) -> str:
    """The chat this agent should treat as home, read with its own new grant.

    Read as the AGENT, not as the account: the grant is what decides reach, so
    asking with it is the same question the gateway will ask on its first
    connection -- and an empty answer here is a grant that would have arrived
    at a silent agent instead.
    """
    transport = HttpCloudTransport.from_environment(
        {"PLOW_API_BASE": base, "PLOW_API_TOKEN": token}
    )
    listing = transport.request("GET", "/v1/chats")
    rows = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(rows, list) or not rows:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            f"the minted grant reaches no chat on {line_uid}",
            "that line must carry a chat this account owns before the agent has a home",
        )

    # The 1:1 is the home: cron and unprompted output land there, and a group
    # would put the owner's private deliveries in front of every member.
    def members(row: object) -> int:
        participants = row.get("participants") if isinstance(row, dict) else None
        if not isinstance(participants, list):
            return 0
        return sum(1 for p in participants if isinstance(p, dict) and p.get("type") == "member")

    ones = [row for row in rows if members(row) == 1]
    # A line can carry several one-to-one chats -- one per person who has texted
    # that number. `min()` broke that tie by whatever order the API happened to
    # return, so provisioning could silently pick ANOTHER CONTACT'S DM as the
    # home and deliver this owner's cron output and private replies into it.
    # There is nothing in the listing that says which of them is the owner's, so
    # the honest answer is to refuse and make the caller name it.
    if len(ones) != 1:
        raise AmbiguousHome(
            ErrorCode.INVALID_ARGUMENT,
            f"{line_uid} carries {len(ones)} one-to-one chats, so the home is ambiguous",
            "name it explicitly with 'agent-mgr set-home <name> <cht_...>' after provisioning, "
            "or provision against a line that serves one person",
        )
    uid = ones[0].get("uid") if isinstance(ones[0], dict) else None
    if not isinstance(uid, str) or not uid:
        raise AgentMgrError(ErrorCode.INVALID_RESPONSE, "chat listing carried no uid")
    return uid


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
    if sys.stdin.isatty():
        token = getpass.getpass("DOMO_MCP_TOKEN: ", stream=sys.stderr).strip()
    else:
        print("DOMO_MCP_TOKEN: ", end="", file=sys.stderr)
        token = sys.stdin.readline().strip()
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
        raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, "DOMO_MCP_TOKEN is REVOKED")
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
