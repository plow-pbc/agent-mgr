from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import termios
from pathlib import Path

from .artifacts import Artifact, fetch, validate_revision
from .boot_contract import (
    CURRENT_HOME,
    credentials_host_path,
    read_plow_credentials,
    require_home_target,
    require_running_contract_matches,
)
from .deploy import reload_if_running
from .errors import AgentMgrError, ErrorCode
from .files import atomic_write, dotenv_declares, dotenv_read, read_regular_text
from .local import compose, require_own_home, require_running, resolve_guard
from .models import ResolvedAgent
from .registry import Registry

ROOT = Path(__file__).resolve().parent.parent


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
    container_id = require_running(agent, registry)
    target = require_running_contract_matches(agent, container_id)
    return compose(
        agent,
        [
            "exec",
            "-T",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--env",
            f"HOME={target}",
            "hermes",
            "/opt/hermes/.venv/bin/python3",
            "-",
            "--spec-json",
            agent.cron_spec.read_text(),
        ],
        stdin=(ROOT / "lib" / "cron-sync.py").read_text(),
    ).returncode


def activate(agent: ResolvedAgent, registry: Registry, line_uid: str) -> int:
    """Mint this agent's Plow credential into its durable two-key file.

    `plow-agents` owns minting -- it asks the server for the assistant role by
    naming a line, revokes the key the file already named before writing the
    new one, and writes atomically at 0600. agent-mgr's job is only to say
    WHICH file. The credential never touches the home dotenv: plow-init strips
    those keys on every boot, and the dotenv is root-owned besides (#163).
    """
    require_own_home(agent, registry)
    if not agent.home.is_dir():
        raise AgentMgrError(
            ErrorCode.IO_ERROR, f"no {agent.home} -- run 'agent-mgr deploy {agent.name}' first"
        )
    if shutil.which("plow-agents") is None:
        raise AgentMgrError(
            ErrorCode.IO_ERROR,
            "plow-agents is not on PATH -- clone plow-pbc/plow-agents and add its bin/ "
            "to PATH, then run 'plow-agents login' once on this machine",
        )
    if require_home_target(agent) != CURRENT_HOME:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{agent.name} boots the legacy contract, which never mounts the credential "
            f"file this writes -- minting would revoke its live key for a file it cannot "
            f"read. Move it to a current-contract base first (see #130).",
        )
    destination = credentials_host_path(agent)
    result = subprocess.run(
        ["plow-agents", "mint", line_uid, "--credential-file", str(destination)],
        check=False,
    )
    if result.returncode:
        return result.returncode
    reload_if_running(agent, registry, "the credential just minted")
    return 0


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


# The relay probe, run by `sh -s` inside the container. Both credential sources
# exist only in there: an instance override's env_file lands in the container's
# environment and never on the host, while the home dotenv is what hermes loads
# over the top of it (`hermes_cli/env_loader.py` calls load_dotenv with
# override=True, which keys off PRESENCE -- a dotenv key that is declared but
# blank still clobbers the container's value to "" there). The prelude below
# matches that: it emits DOTENV_UID/DOTENV_TOK only for a key the dotenv
# actually DECLARES, so the probe falls through to the container's own value
# only when hermes would too.
#
# The script arrives on stdin rather than in argv, and the bearer reaches curl
# through a PIPE rather than a file at rest in the container's filesystem -- so
# the credential is absent from `ps` on the host and inside the container
# alike, and from disk too. `printf` is still a shell builtin, so piping its
# output costs nothing on that front -- and unlike `echo` in dash (the agent
# image's /bin/sh), it never expands a backslash the token happens to carry.
LATCH_PROBE = """\
UID_V="${DOTENV_UID-${DOMO_DEVICE_UID:-}}"
TOK="${DOTENV_TOK-${DOMO_MCP_TOKEN:-}}"
case "$UID_V" in *[![:space:]]*) ;; *) echo UNSET:DOMO_DEVICE_UID; exit 0 ;; esac
case "$TOK" in *[![:space:]]*) ;; *) echo UNSET:DOMO_MCP_TOKEN; exit 0 ;; esac
printf 'header = "Authorization: Bearer %s"\\n' "$TOK" | curl -sS --max-time 30 \\
  -o /dev/null -w '%{http_code}' --config - \\
  -X POST "https://api.plow.co/v1/relay/devices/$UID_V/mcp" \\
  -H 'Content-Type: application/json' \\
  -H 'Accept: application/json, text/event-stream' \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
"""


def check_latch(agent: ResolvedAgent, registry: Registry) -> int:
    dotenv, installed = agent.home / ".env", agent.home / "config.yaml"
    if not installed.is_file():
        raise AgentMgrError(ErrorCode.IO_ERROR, f"no {installed} -- run deploy first")
    if not config_declares_latch(installed):
        print(f"no latch configured for {agent.name} -- its config declares no latch server")
        return 0
    require_running(agent, registry)
    # A dotenv that is not there contributes nothing; one that is there and
    # cannot be READ still raises out of read_regular_text, because a
    # permission problem is not an unset credential. A key it does not
    # DECLARE also contributes nothing -- omitting the line leaves the
    # variable unset in the container's sh, which is what LATCH_PROBE's
    # fallback keys off.
    prelude = "".join(
        f"{name}={shlex.quote(dotenv_read(dotenv, key))}\n"
        for name, key in (("DOTENV_UID", "DOMO_DEVICE_UID"), ("DOTENV_TOK", "DOMO_MCP_TOKEN"))
        if dotenv.exists() and dotenv_declares(dotenv, key)
    )
    response = compose(
        agent,
        ["exec", "-T", "hermes", "sh", "-s"],
        capture=True,
        stdin=prelude + LATCH_PROBE,
    )
    code = response.stdout.strip()
    if code == "200":
        print(f"latch reachable from {agent.name}'s container (HTTP 200)")
        return 0
    if code.startswith("UNSET:"):
        key = code.partition(":")[2]
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"{key} is empty for {agent.name} -- neither {dotenv} nor the container's "
            f"environment defines it. Mint the pair on the Mac, then: "
            f"agent-mgr set-latch {agent.name}",
        )
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
    # No contract derivation: the credential lives in one file under either
    # one, so which contract the running container was created under does not
    # decide where its token is read from. Still requires a RUNNING container,
    # because the curl runs inside it.
    require_running(agent, registry)
    base, token = read_plow_credentials(agent)
    if not token:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"PLOW_AGENT_TOKEN is empty for {agent.name} -- "
            f"run 'agent-mgr activate {agent.name} <line-uid>' first",
        )
    base = base or "https://api.plow.co"
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
    container_id = require_running(agent, registry)
    target = require_running_contract_matches(agent, container_id)
    skill = f"{target}/skills/productivity/plow-connectors/plow_connector.py"
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
            skill,
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
                skill,
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
