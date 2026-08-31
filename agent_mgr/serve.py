"""Plow's cloud-agent API, served against local containers.

The CLI reached parity first: one verb set over both targets. This is the other
half -- the same *wire* contract, so a caller written against
`POST /v1/agents/cloud` drives a container on this host without knowing it is
not exe. The paths, the request bodies and the resource shape are Plow's, read
from its published spec; only the substrate underneath differs.

Where the substrates genuinely differ, this says so rather than inventing a
mapping:

- **exe unpacks an image; a local agent needs a checkout.** `CreateCloudAgentRequest`
  carries `{line_uid, name?, provider?}` and no repository, because on exe there
  is nothing to point at. So POST provisions a name that is already registered
  and refuses one that is not, naming the register command. Same wire shape,
  same status codes, and the one asymmetry is stated in the error rather than
  guessed at.
- **DELETE stops the container and drops the registry row, but keeps the home.**
  On exe the tenant *is* the agent, so answering with a deleted resource while
  leaving a row the next GET resolves would report a deletion that did not
  happen. The credentials, the checkpoint and the cron schedule live in
  `~/.hermes-<name>` on this host, and no HTTP request may destroy them --
  `register` brings the agent back against the same home.

Bearer auth is mandatory and the bind is loopback by default: these routes
start and stop containers, so a token-less listener would be a remote shell for
anything that can reach the port.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .cloud_http import HttpCloudTransport
from .cloud_models import (
    CloudAgentResource,
    CloudStatus,
    CreateCloudAgentRequest,
    FailureCode,
    UpdateCloudAgentLineRequest,
)
from .commands import dotenv_read
from .descriptor import resolve_agent
from .errors import AgentMgrError, ErrorCode
from .local import (
    compose,
    require_container_ours,
    require_own_home,
    resolve_guard,
    transition,
)
from .models import JsonValue, ResolvedAgent
from .registry import Registry

# `PLOW_CHAT_CHAT_UID` is the home chat activation writes. It is the honest
# answer to "which chats does this agent serve": a local gateway's reach is its
# credential's grant, and the dotenv is the only part of that on this host.
# `PLOW_HOME_CHANNEL` is the canonical name; `PLOW_CHAT_CHAT_UID` is the legacy
# spelling `migrate-plugin-env` moves off. Read canonical first and fall back,
# or a canonical-only agent reports no chats at all -- which also made an
# idempotent PUT of its real set look like a change and get refused.
HOME_CHAT_KEYS = ("PLOW_HOME_CHANNEL", "PLOW_CHAT_CHAT_UID")
# There is no `PLOW_AGENT_LINE`: nothing writes one, so checking a request
# against it accepted every line on POST while the identical PUT refused. The
# line an agent actually serves is the one its HOME CHAT sits on, and that is
# read from Plow with the agent's own credential -- the same question the
# gateway answers on connect, rather than a key invented for this facade.
LINE_LOOKUP_TIMEOUT_SECONDS = 10
PROVIDER = "local:docker"
ROUTE = re.compile(r"^/v1/agents/cloud(?:/(?P<agent_id>[^/]+)(?P<line>/line)?)?$")


@dataclass(frozen=True, slots=True)
class ApiError(Exception):
    status: int
    message: str

    def body(self) -> dict[str, JsonValue]:
        return {"detail": self.message}


def _dotenv_chats(agent: ResolvedAgent) -> tuple[str, ...]:
    value = _dotenv_value(agent, HOME_CHAT_KEYS)
    return (value,) if value else ()


def _dotenv_value(agent: ResolvedAgent, keys: tuple[str, ...]) -> str:
    """First key in `keys` that the dotenv declares, through the canonical seam.

    This parsed the file itself and swallowed read failures as "", which the
    callers then read as "uncredentialed" -- so an unreadable dotenv made POST
    skip line validation entirely and answer 202 for a line it never checked.
    It also matched keys with `.strip()`, disagreeing with `dotenv_read` about
    whitespace, so the two could see different values in the same file.

    `dotenv_read` is that one definition, and it fails loudly on an unreadable
    file. One reader, one spelling, one failure mode.
    """
    path = agent.home / ".env"
    for key in keys:
        value = dotenv_read(path, key)
        if value:
            return value
    return ""


class LineUnknown(Exception):
    """The agent has no credential yet -- there is no line to compare against."""


def _agent_line(agent: ResolvedAgent) -> tuple[str, ...]:
    """The line this agent's home chat sits on, asked of Plow as the agent.

    Three outcomes, and collapsing them was the bug: `()` used to mean both
    "not credentialed yet" and "the API did not answer", so CREATE read an
    outage as "no mismatch" and returned 202 for the WRONG line while PUT of
    the right one refused. Now `LineUnknown` says the agent has no credential
    (nothing to compare, proceed), and an API failure raises `ApiError` (refuse,
    because a mismatch cannot be ruled out).
    """
    dotenv = agent.home / ".env"
    if not dotenv.exists():
        # ABSENT is uncredentialed: a fresh home before `restore` has one, and
        # that is a legitimate state, not a failure.
        raise LineUnknown
    try:
        home = _dotenv_value(agent, HOME_CHAT_KEYS)
        token = _dotenv_value(agent, ("PLOW_AGENT_TOKEN", "PLOW_CHAT_TOKEN"))
    except AgentMgrError as error:
        # PRESENT but unreadable is not. Collapsing the two let POST skip line
        # validation on a file it could not read and answer 202 regardless.
        raise ApiError(502, f"could not read {agent.name}'s dotenv: {error.message}") from None
    # `PLOW_API_BASE` absent is the ordinary case, not a failure: the gateway
    # falls back to the public API and so does this.
    base = _dotenv_value(agent, ("PLOW_API_BASE", "PLOW_CHAT_BASE_URL")) or "https://api.plow.co"
    if not home or not token:
        raise LineUnknown
    try:
        transport = HttpCloudTransport.from_environment(
            {"PLOW_API_BASE": base, "PLOW_API_TOKEN": token}
        )
        chat = transport.request("GET", f"/v1/chats/{home}")
    except AgentMgrError as error:
        raise ApiError(
            502, f"could not read {agent.name}'s line from Plow: {error.message}"
        ) from None
    if not isinstance(chat, dict):
        raise ApiError(502, f"Plow returned no readable chat for {agent.name}'s home")
    # The SELF participant, the way `narrow_chat_credential` already selects it.
    # Taking the first agent participant picked a sibling's line in any chat
    # holding more than one agent -- and a wrong line makes a valid POST or PUT
    # 409 against a number the agent really does serve.
    agents = [
        participant
        for participant in chat.get("participants") or []
        if isinstance(participant, dict) and participant.get("type") == "agent"
    ]
    mine = [participant for participant in agents if participant.get("relationship") == "self"]
    current = mine[0] if len(mine) == 1 else agents[0] if len(agents) == 1 else None
    line = current.get("line") if current is not None else None
    uid = line.get("uid") if isinstance(line, dict) else None
    if isinstance(uid, str) and uid:
        return (uid,)
    # Malformed, or several agents with no `self` among them: either way this
    # cannot say which line is ours, and guessing is what the probe caught.
    raise ApiError(502, f"{agent.name}'s home chat does not identify exactly one agent line")


def _status(agent: ResolvedAgent) -> tuple[CloudStatus, FailureCode | None]:
    """Docker's answer, mapped onto the four public statuses.

    A registered agent whose container has never been created is `provisioning`
    rather than `failed`: it is the state a fresh exe tenant is in between the
    202 and first boot, and the caller polls the same way out of both.
    """
    running = compose(agent, ["ps", "--status", "running", "--quiet", "hermes"], capture=True)
    if running.returncode:
        return CloudStatus.FAILED, FailureCode.PROVIDER_UNREACHABLE
    if running.stdout.strip():
        return CloudStatus.RUNNING, None
    existing = compose(agent, ["ps", "--all", "--quiet", "hermes"], capture=True)
    if existing.returncode:
        return CloudStatus.FAILED, FailureCode.PROVIDER_UNREACHABLE
    if existing.stdout.strip():
        return CloudStatus.FAILED, FailureCode.SETUP_FAILED
    return CloudStatus.PROVISIONING, None


class LocalCloudApi:
    """The five routes, against this host's registry."""

    def __init__(self, registry: Registry, root: Any) -> None:
        self.registry = registry
        self.root = root
        # One provisioning state per agent, behind one lock. Without it the
        # worker was untracked: a second POST started a second `up` against the
        # same home, a DELETE could remove the row while a worker was still
        # bringing that agent up -- leaving a gateway nothing owns -- and a
        # failed pull was invisible, so GET answered `provisioning` forever.
        # Reentrant: `_provisioning()` is called while holding it and reaches
        # `_resource()`, which takes it again to read the failure map. A plain
        # Lock deadlocked the duplicate-POST path outright.
        self._lock = threading.RLock()
        self._starting: set[str] = set()
        self._deleting: set[str] = set()
        # What the caller asked for, so an uncredentialed agent's resource can
        # report the line it was created against instead of an empty grant the
        # shared parser refuses.
        self._requested_line: dict[str, str] = {}
        self._failed: dict[str, FailureCode] = {}

    def _agent(self, agent_id: str) -> ResolvedAgent:
        try:
            return resolve_agent(agent_id, self.registry, self.root)
        except AgentMgrError as error:
            if error.code is ErrorCode.AGENT_NOT_FOUND:
                raise ApiError(404, "Cloud agent not found") from None
            raise ApiError(400, error.message) from None

    def _resource(self, agent: ResolvedAgent, *, deleted: bool = False) -> CloudAgentResource:
        status, failure = (None, None) if deleted else _status(agent)
        with self._lock:
            recorded = self._failed.get(agent.name)
            starting = agent.name in self._starting
        if not deleted and recorded is not None:
            # A recorded failure outranks `compose ps`. It used to be suppressed
            # whenever docker reported RUNNING -- which is exactly the case a
            # foreign container in a reused project produces.
            status, failure = CloudStatus.FAILED, recorded
        elif not deleted and starting and status is not CloudStatus.RUNNING:
            status, failure = CloudStatus.PROVISIONING, None
        return CloudAgentResource(
            agent_id=agent.name,
            # Never empty: `CloudAgentResource` requires a non-empty grant, so
            # an uncredentialed agent answering `[]` produced a body Plow's own
            # parser -- and therefore every client -- refuses. Before the
            # credential lands, the line it was created against IS its grant.
            chat_uids=_dotenv_chats(agent) or self._created_grant(agent),
            # A local agent has no published address -- exe's `url` is the
            # tenant's, and a fabricated https:// one would read as reachable.
            # The container is what a caller can actually act on from here.
            url=f"docker://{agent.container}",
            provider=PROVIDER,
            status=status,
            failure_code=failure,
        )

    def list(self) -> list[dict[str, JsonValue]]:
        resources = []
        for entry in self.registry.entries():
            if entry.is_cloud:
                continue
            resources.append(self._resource(self._agent(entry.name)).to_json())
        return resources

    def get(self, agent_id: str) -> dict[str, JsonValue]:
        return self._resource(self._agent(agent_id)).to_json()

    def create(self, payload: object) -> dict[str, JsonValue]:
        request = CreateCloudAgentRequest.from_json(payload)
        try:
            self.registry.entry(request.name)
        except AgentMgrError:
            raise ApiError(
                400,
                f"{request.name} is not registered on this host. exe unpacks an image; a "
                f"local agent needs a checkout -- run 'agent-mgr register {request.name} <dir>' "
                "first, then repeat this request.",
            ) from None
        agent = self._agent(request.name)
        # `provider` is not decoration: a request naming exe must not quietly
        # start a container here, or a caller that thinks it provisioned a cloud
        # tenant gets a local one and never learns the difference.
        # Omitting it is valid in the shared request model, and the facade knows
        # its own substrate -- refusing there made a legal Plow request fail
        # against this host for naming nothing at all. Only a WRONG provider is
        # a mistake worth stopping.
        if request.provider is not None and request.provider != PROVIDER:
            raise ApiError(
                400,
                f"this host provisions {PROVIDER!r}; {request.provider!r} belongs to another provider",
            )
        # The same guards `agent-mgr up` takes, for the same reason. Reaching
        # for compose directly skipped ownership, container identity and the
        # transition veto -- so a copied descriptor could start a second gateway
        # against a sibling's home, or a reused project stop a live agent, from
        # an HTTP request that never touched those checks.
        # Line and admission checks are SLOW -- one reads Plow over the network
        # -- and they used to run outside the lock. A POST paused in them could
        # resume after a DELETE had removed the registry row and cleared
        # `_deleting`, and then start a gateway nothing on this host owns. The
        # whole decision, from the checks through the reservation, is one
        # critical section now; the slow part is the price of not racing.
        with self._lock:
            if agent.name in self._deleting:
                raise ApiError(409, f"{agent.name} is being deleted; retry once it settles")
            try:
                held = _agent_line(agent)
            except LineUnknown:
                # Not credentialed yet. The line still has to be RECORDED, or
                # POST answers 202 having ignored `line_uid` entirely -- the
                # caller would believe it selected a number it did not.
                held = ()
            if held and (request.line_uid,) != held:
                raise ApiError(
                    409,
                    f"{request.name} is bound to {held[0]}, not {request.line_uid}; a local "
                    "agent's line comes from the credential minted for it",
                )
            self._admit(agent)
            # Re-checked after the slow work: a DELETE may have landed while
            # this request was reading Plow.
            if agent.name in self._deleting:
                raise ApiError(409, f"{agent.name} is being deleted; retry once it settles")
            if agent.name in self._starting:
                # Coalesced, not queued: the answer is the same resource and
                # the caller polls the same GET either way.
                return self._provisioning(agent)
            self._requested_line[agent.name] = request.line_uid
            # Build the ANSWER before starting anything. Serialization reads the
            # dotenv, and on a fresh home that raised after `_bring_up` was
            # already running -- the container came up and the caller was told
            # 400, with nothing naming what had started.
            answer = self._provisioning(agent)
            self._starting.add(agent.name)
            self._failed.pop(agent.name, None)
        # Then answer, and bring it up behind the response. `POST` is 202 in
        # Plow's contract precisely because provisioning outlasts a request --
        # exe's unpack "can outlast the sixty seconds the gateway allows", and a
        # first `docker pull` here is minutes. Waiting made the one-click create
        # time out in the client while the work was succeeding; the caller polls
        # GET out of `provisioning`, the same loop it runs against exe.
        threading.Thread(
            target=self._bring_up, args=(agent,), name=f"up:{agent.name}", daemon=True
        ).start()
        return answer

    def _created_grant(self, agent: ResolvedAgent) -> tuple[str, ...]:
        with self._lock:
            requested = self._requested_line.get(agent.name)
        return (f"line:{requested}",) if requested else ("line:unassigned",)

    def _provisioning(self, agent: ResolvedAgent) -> dict[str, JsonValue]:
        answer = self._resource(agent).to_json()
        answer["status"] = CloudStatus.PROVISIONING.value
        answer["failure_code"] = None
        return answer

    def _admit(self, agent: ResolvedAgent) -> None:
        """Ownership and identity, before the answer.

        Separate from `_guarded` because these must refuse with a status code
        while the caller is still listening; the transition itself happens after
        the 202, where there is nobody left to raise to.
        """
        try:
            require_own_home(agent, self.registry)
            resolve_guard(agent, self.registry)
            # Container identity too. Without it a reused Compose project
            # mounting ANOTHER agent's home was admitted here, the background
            # transition then refused it, and `_status()` read `compose ps` and
            # reported that foreign container as this agent RUNNING.
            require_container_ours(agent)
        except AgentMgrError as error:
            raise ApiError(409, error.message) from None

    def _bring_up(self, agent: ResolvedAgent) -> None:
        """The work behind a 202. Failures land in the resource, not a response.

        There is nobody to raise to once the answer is sent, and a second
        channel for it would be a second thing to keep correct: the next GET
        reports `failed`, which is how a caller already learns about an exe
        provision that did not finish.
        """
        try:
            self._guarded(agent, ["up", "-d"], f"could not start {agent.name}")
        except ApiError:
            # Recorded, not swallowed. There is nobody to raise to once the 202
            # is sent, and a caller polling GET would otherwise read
            # `provisioning` forever after a pull that failed.
            with self._lock:
                self._failed[agent.name] = FailureCode.SETUP_FAILED
        finally:
            with self._lock:
                self._starting.discard(agent.name)

    def _guarded(self, agent: ResolvedAgent, argv: Sequence[str], failure: str) -> None:
        try:
            require_own_home(agent, self.registry)
            resolve_guard(agent, self.registry)
            code = transition(agent, argv)
        except AgentMgrError as error:
            raise ApiError(409, error.message) from None
        if code:
            raise ApiError(502, f"{failure}: docker compose exited {code}")

    def update_line(self, agent_id: str, payload: object) -> dict[str, JsonValue]:
        request = UpdateCloudAgentLineRequest.from_json(payload)
        agent = self._agent(agent_id)
        try:
            current = _agent_line(agent)
        except LineUnknown:
            current = ()
        if (request.line_uid,) == current:
            # Asking for the line it already serves is not a write. Answering
            # 200 keeps an idempotent caller -- one that PUTs desired state on
            # every reconcile -- working against a host that already matches.
            return self._resource(agent).to_json()
        # Refused rather than half-done: a local agent's line grant lives in the
        # credential minted for it, and this process must not forge one.
        # Answering 200 to a write that changed nothing is the failure to avoid.
        raise ApiError(
            409,
            f"{agent.name} is bound to its minted credential's line. Activation cannot "
            "re-point it -- Plow assigns the line and the caller does not choose it -- so "
            "moving an agent to a named line means minting a credential for that line "
            "directly, not a request over HTTP",
        )

    def delete(self, agent_id: str) -> dict[str, JsonValue]:
        agent = self._agent(agent_id)
        # The lock is held from the check through the row removal, not just over
        # the check: releasing it in between let a concurrent POST reserve and
        # launch a startup in the gap, and DELETE then dropped the row out from
        # under a gateway that was already coming up -- one running against a
        # home nothing on this host owns.
        with self._lock:
            if agent.name in self._starting:
                raise ApiError(409, f"{agent.name} is still provisioning; retry once it settles")
            self._deleting.add(agent.name)
        try:
            resource = self._resource(agent, deleted=True)
            self._guarded(agent, ["down"], f"could not stop {agent_id}")
            # The row goes with the container. exe's DELETE makes the agent stop
            # existing, and answering with a deleted resource while leaving a
            # row the next GET resolves would report a deletion that did not
            # happen. The HOME stays -- credentials, checkpoint and cron live
            # there and no HTTP request may destroy them; `register` brings the
            # agent back against the same home.
            self.registry.remove(agent.name)
            return resource.to_json()
        finally:
            self._deleting.discard(agent.name)


def _without_control_token() -> None:
    """Drop the server's bearer from this process's environment.

    `transition` runs an agent's own pre-transition hook, which inherits the
    server's environment -- so the fleet-wide control token was handed to
    agent-specific code on every create and delete. The handler compares the
    header against the value it captured at startup, so the variable itself is
    not needed after that.
    """
    os.environ.pop("AGENT_MGR_SERVE_TOKEN", None)


def build_handler(api: LocalCloudApi, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-mgr"

        def _reply(self, status: int, body: dict[str, JsonValue] | list[Any]) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _authorised(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {token}"

        def _payload(self) -> object:
            length = int(self.headers.get("Content-Length", "0"))
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                raise ApiError(400, "invalid JSON body") from None

        def _dispatch(self) -> tuple[int, dict[str, JsonValue] | list[Any]]:
            match = ROUTE.match(self.path)
            if not match:
                raise ApiError(404, "Not Found")
            agent_id, line = match.group("agent_id"), match.group("line")
            method = self.command
            if line:
                if method != "PUT":
                    raise ApiError(405, "Method Not Allowed")
                return 200, api.update_line(str(agent_id), self._payload())
            if agent_id is None:
                if method == "GET":
                    return 200, api.list()
                if method == "POST":
                    # 202 like Plow's: the container is starting behind this
                    # answer exactly as a tenant is, and a caller that polls
                    # GET works unchanged against either.
                    return 202, api.create(self._payload())
                raise ApiError(405, "Method Not Allowed")
            if method == "GET":
                return 200, api.get(agent_id)
            if method == "DELETE":
                return 200, api.delete(agent_id)
            raise ApiError(405, "Method Not Allowed")

        def _serve(self) -> None:
            if not self._authorised():
                self._reply(401, {"detail": "unauthorized"})
                return
            try:
                status, body = self._dispatch()
            except ApiError as error:
                self._reply(error.status, error.body())
            except AgentMgrError as error:
                self._reply(400, {"detail": error.message})
            else:
                self._reply(status, body)

        do_GET = do_POST = do_PUT = do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:
            # The default handler logs the request line to stderr, and a path
            # here carries an agent name. Quiet by default; the caller's own
            # logs are where a request belongs.
            return

    return Handler


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _tailscale_addresses() -> frozenset[str]:
    """This machine's own Tailscale addresses, or empty when there is no tailnet.

    Asked of `tailscale` rather than matched on `100.64/10`: that range is
    shared CGNAT, not Tailscale's alone, so a prefix test would also accept a
    carrier-assigned address on an ordinary network -- exactly the cleartext
    path the loopback rule exists to keep a replayable bearer off.
    """
    try:
        result = subprocess.run(
            ["tailscale", "ip"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode:
        return frozenset()
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def resolve_bind(host: str) -> str:
    """The ADDRESS to bind, or a refusal. Never the name.

    Validating a hostname and then handing the *name* to the socket resolves it
    twice, and a name carrying both an owned tailnet address and a LAN or public
    one passes the check and then binds the unsafe one -- the exact exposure the
    rule exists to prevent. So this returns the single address it approved, and
    the caller binds that.

    A tailnet is the one non-loopback case the objection does not cover: the
    bearer still travels in plain HTTP, but WireGuard between two authenticated
    peers leaves no on-path position to replay from. Everything else stays
    refused, because a token cannot fix a cleartext path.
    """
    if host in LOOPBACK_HOSTS:
        return host
    own = _tailscale_addresses()
    if host in own:
        return host
    # A MagicDNS name is the spelling clients prefer -- a bare 100.x cannot be
    # told apart from carrier CGNAT by the caller either -- so resolve it, and
    # accept ONLY when every address it names is ours. One unsafe address in the
    # set is enough to refuse: which one the socket would pick is not ours to
    # assume.
    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        resolved = set()
    if resolved and resolved <= own:
        # getaddrinfo's sockaddr is (host, port) for v4 and a 4-tuple for v6;
        # the address is element 0 either way, and str() keeps mypy honest.
        return str(sorted(resolved)[0])
    raise AgentMgrError(
        ErrorCode.INVALID_ARGUMENT,
        f"refusing to bind {host}: this serves plain HTTP with a replayable bearer, and "
        "these routes start and stop containers",
        "bind loopback or this machine's tailnet address, or forward the port: "
        "ssh -L <port>:127.0.0.1:<port> <host>",
    )


def serve(registry: Registry, root: Any, host: str, port: int) -> int:
    token = os.environ.get("AGENT_MGR_SERVE_TOKEN", "").strip()
    if not token:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            "AGENT_MGR_SERVE_TOKEN is unset",
            "these routes start and stop containers; set a bearer token before serving",
        )
    host = resolve_bind(host)
    handler = build_handler(LocalCloudApi(registry, root), token)
    _without_control_token()
    with ThreadingHTTPServer((host, port), handler) as httpd:
        print(
            f"serving Plow's cloud-agent API for local agents on http://{host}:{httpd.server_address[1]}"
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0
