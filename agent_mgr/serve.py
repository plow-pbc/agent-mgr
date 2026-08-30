"""Plow's cloud-agent API, served against local containers.

The CLI reached parity first: one verb set over both targets. This is the other
half -- the same *wire* contract, so a caller written against
`POST /v1/agents/cloud` drives a container on this host without knowing it is
not exe. The paths, the request bodies and the resource shape are Plow's, read
from its published spec; only the substrate underneath differs.

Where the substrates genuinely differ, this says so rather than inventing a
mapping:

- **exe unpacks an image; a local agent needs a checkout.** `CreateCloudAgentRequest`
  carries `{name, provider, chat_uids}` and no repository, because on exe there
  is nothing to point at. So POST provisions a name that is already registered
  and refuses one that is not, naming the register command. Same wire shape,
  same status codes, and the one asymmetry is stated in the error rather than
  guessed at.
- **DELETE stops the container and keeps the home.** On exe the tenant *is* the
  agent and deleting it takes everything; here the credentials, the checkpoint
  and the cron schedule live in `~/.hermes-<name>` on this host, and no HTTP
  request should be able to destroy them. Removing the row is `unregister`,
  which stays a deliberate local act.

Bearer auth is mandatory and the bind is loopback by default: these routes
start and stop containers, so a token-less listener would be a remote shell for
anything that can reach the port.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .cloud_models import (
    CloudAgentResource,
    CloudStatus,
    CreateCloudAgentRequest,
    FailureCode,
    UpdateCloudAgentChatsRequest,
)
from .descriptor import resolve_agent
from .errors import AgentMgrError, ErrorCode
from .local import compose, require_own_home, resolve_guard, transition
from .models import JsonValue, ResolvedAgent
from .registry import Registry

# `PLOW_CHAT_CHAT_UID` is the home chat activation writes. It is the honest
# answer to "which chats does this agent serve": a local gateway's reach is its
# credential's grant, and the dotenv is the only part of that on this host.
HOME_CHAT_KEY = "PLOW_CHAT_CHAT_UID"
PROVIDER = "local:docker"
ROUTE = re.compile(r"^/v1/agents/cloud(?:/(?P<agent_id>[^/]+)(?P<chats>/chats)?)?$")


@dataclass(frozen=True, slots=True)
class ApiError(Exception):
    status: int
    message: str

    def body(self) -> dict[str, JsonValue]:
        return {"detail": self.message}


def _dotenv_chats(agent: ResolvedAgent) -> tuple[str, ...]:
    path = agent.home / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() == HOME_CHAT_KEY and value.strip():
            return (value.strip(),)
    return ()


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

    def _agent(self, agent_id: str) -> ResolvedAgent:
        try:
            return resolve_agent(agent_id, self.registry, self.root)
        except AgentMgrError as error:
            if error.code is ErrorCode.AGENT_NOT_FOUND:
                raise ApiError(404, "Cloud agent not found") from None
            raise ApiError(400, error.message) from None

    def _resource(self, agent: ResolvedAgent, *, deleted: bool = False) -> CloudAgentResource:
        status, failure = (None, None) if deleted else _status(agent)
        return CloudAgentResource(
            agent_id=agent.name,
            chat_uids=_dotenv_chats(agent),
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
        if request.provider != PROVIDER:
            raise ApiError(
                400,
                f"this host provisions {PROVIDER!r}; {request.provider!r} belongs to another provider",
            )
        # The same guards `agent-mgr up` takes, for the same reason. Reaching
        # for compose directly skipped ownership, container identity and the
        # transition veto -- so a copied descriptor could start a second gateway
        # against a sibling's home, or a reused project stop a live agent, from
        # an HTTP request that never touched those checks.
        self._guarded(agent, ["up", "-d"], f"could not start {request.name}")
        return self._resource(agent).to_json()

    def _guarded(self, agent: ResolvedAgent, argv: Sequence[str], failure: str) -> None:
        try:
            require_own_home(agent, self.registry)
            resolve_guard(agent, self.registry)
            code = transition(agent, argv)
        except AgentMgrError as error:
            raise ApiError(409, error.message) from None
        if code:
            raise ApiError(502, f"{failure}: docker compose exited {code}")

    def update_chats(self, agent_id: str, payload: object) -> dict[str, JsonValue]:
        request = UpdateCloudAgentChatsRequest.from_json(payload)
        agent = self._agent(agent_id)
        if tuple(request.chat_uids) == _dotenv_chats(agent):
            # Asking for the set it already has is not a write. Answering 200
            # here keeps an idempotent caller -- one that PUTs its desired state
            # on every reconcile -- from failing against a host that already
            # matches it.
            return self._resource(agent).to_json()
        # Refused rather than half-done: on exe the chat set lives in the
        # credential the API mints, and here it is written by `activate` into a
        # dotenv this process must not forge. Answering 200 to a write that
        # changed nothing is the failure worth avoiding.
        raise ApiError(
            409,
            "a local agent's chat grant is its activation credential, minted by "
            "'agent-mgr activate <name>' from the owner's phone -- it cannot be set over HTTP",
        )

    def delete(self, agent_id: str) -> dict[str, JsonValue]:
        agent = self._agent(agent_id)
        resource = self._resource(agent, deleted=True)
        self._guarded(agent, ["down"], f"could not stop {agent_id}")
        # The row goes with the container. exe's DELETE makes the agent stop
        # existing, and answering with a deleted resource while leaving a row
        # that the next GET happily resolves would report a deletion that did
        # not happen. The HOME is what stays -- credentials, checkpoint and cron
        # schedule live there, and no HTTP request may destroy them; `register`
        # brings the agent back against the same home.
        self.registry.remove(agent.name)
        return resource.to_json()


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
            agent_id, chats = match.group("agent_id"), match.group("chats")
            method = self.command
            if chats:
                if method != "PUT":
                    raise ApiError(405, "Method Not Allowed")
                return 200, api.update_chats(str(agent_id), self._payload())
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


def serve(registry: Registry, root: Any, host: str, port: int) -> int:
    token = os.environ.get("AGENT_MGR_SERVE_TOKEN", "").strip()
    if not token:
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            "AGENT_MGR_SERVE_TOKEN is unset",
            "these routes start and stop containers; set a bearer token before serving",
        )
    # A bearer is not enough to earn a public bind. These routes start and stop
    # containers on this host, and `0.0.0.0` puts them in front of every device
    # on the network -- including one that only has to guess a token. Reaching
    # them from elsewhere is what SSH forwarding is for; an explicit opt-out
    # exists so the refusal is a decision rather than a wall.
    if host not in LOOPBACK_HOSTS and os.environ.get("AGENT_MGR_SERVE_PUBLIC") != "1":
        raise AgentMgrError(
            ErrorCode.INVALID_ARGUMENT,
            f"refusing to bind {host}: these routes start and stop containers",
            "forward the loopback port over SSH, or set AGENT_MGR_SERVE_PUBLIC=1 to accept the exposure",
        )
    handler = build_handler(LocalCloudApi(registry, root), token)
    with ThreadingHTTPServer((host, port), handler) as httpd:
        print(
            f"serving Plow's cloud-agent API for local agents on http://{host}:{httpd.server_address[1]}"
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            return 0
    return 0
