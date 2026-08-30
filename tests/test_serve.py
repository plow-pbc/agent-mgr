"""The served half of parity: Plow's wire contract, against local containers.

These drive the real handler over a real socket rather than calling the API
object directly -- the routing, the auth gate and the status codes are the
contract, and a test that skips the transport asserts none of them.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest
from pathlib import Path
from types import SimpleNamespace
from agent_mgr.cloud_models import CloudAgentResource
from agent_mgr.registry import Registry
from agent_mgr.serve import LocalCloudApi, build_handler
from conftest import ROOT

TOKEN = "serve-token"


class _Api(LocalCloudApi):
    """Real routing, real request parsing, real guards -- only docker and the
    descriptor are stubbed. A fake that overrode the handlers themselves would
    assert the fake: the provider gate and the chat comparison live in those
    methods, and every probe this file answers is about them.
    """

    def __init__(self, registry: Registry, resources: dict[str, dict[str, object]]) -> None:
        super().__init__(registry, ROOT)
        self.resources = resources
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.home_chats: tuple[str, ...] = ("cht_home",)

    def _agent(self, agent_id: str):  # type: ignore[override]
        from agent_mgr.serve import ApiError

        if agent_id not in self.resources:
            raise ApiError(404, "Cloud agent not found")
        return SimpleNamespace(name=agent_id, container=f"hermes-{agent_id}", home=Path("/nowhere"))

    def _resource(self, agent, *, deleted: bool = False):  # type: ignore[override]
        payload = dict(self.resources[agent.name])
        if deleted:
            payload |= {"status": None, "failure_code": None}
        return SimpleNamespace(to_json=lambda: payload)

    def _guarded(self, agent, argv, failure: str) -> None:  # type: ignore[override]
        (self.started if argv[0] == "up" else self.stopped).append(agent.name)

    def list(self) -> list[dict[str, object]]:  # type: ignore[override]
        return list(self.resources.values())


def _resource(name: str) -> dict[str, object]:
    return {
        "agent_id": name,
        "chat_uids": ["cht_home"],
        "url": f"docker://hermes-{name}",
        "provider": "local:docker",
        "status": "running",
        "failure_code": None,
    }


@pytest.fixture
def api(tmp_path) -> _Api:
    registry = Registry(tmp_path / "agents")
    repo = tmp_path / "life-repo"
    repo.mkdir()
    registry.add("life", repo)
    return _Api(registry, {"life": _resource("life")})


@pytest.fixture
def base(api: _Api) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(api, TOKEN))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _call(
    base: str, method: str, path: str, body: object | None = None, token: str | None = TOKEN
) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_the_listing_is_plows_resource_shape(base: str) -> None:
    """The point of the exercise: a caller written against Plow parses this."""
    status, body = _call(base, "GET", "/v1/agents/cloud")

    assert status == 200
    assert isinstance(body, list)
    # Round-tripped through the real parser, so a drifted field fails here
    # rather than in whatever consumes it later.
    resource = CloudAgentResource.from_json(body[0])
    assert resource.agent_id == "life"
    assert resource.provider == "local:docker"


def test_one_agent_reads_back_by_name(base: str) -> None:
    status, body = _call(base, "GET", "/v1/agents/cloud/life")

    assert status == 200
    assert CloudAgentResource.from_json(body).agent_id == "life"


def test_a_missing_agent_is_404_not_500(base: str) -> None:
    status, body = _call(base, "GET", "/v1/agents/cloud/nobody")

    assert status == 404
    assert body == {"detail": "Cloud agent not found"}


def test_create_answers_202_like_plow_does(base: str, api: _Api) -> None:
    """202, not 200: the container is starting behind the answer exactly as a
    tenant is, so a caller that polls GET works unchanged against either."""
    status, body = _call(
        base,
        "POST",
        "/v1/agents/cloud",
        {"name": "life", "provider": "local:docker", "chat_uids": ["cht_home"]},
    )

    assert status == 202
    assert CloudAgentResource.from_json(body).agent_id == "life"
    assert api.started == ["life"]


def test_create_names_the_one_real_asymmetry(base: str) -> None:
    """exe unpacks an image; a local agent needs a checkout. The wire shape is
    identical, so the difference has to arrive as a message, not a 500."""
    status, body = _call(
        base,
        "POST",
        "/v1/agents/cloud",
        {"name": "ghost", "provider": "local:docker", "chat_uids": ["cht_home"]},
    )

    assert status == 400
    assert "not registered on this host" in body["detail"]


def test_delete_returns_a_null_status_resource(base: str, api: _Api) -> None:
    """Plow's delete response carries `status: null`, and its own parser refuses
    anything else -- so a local delete has to answer the same or the shared
    client rejects it."""
    status, body = _call(base, "DELETE", "/v1/agents/cloud/life")

    assert status == 200
    assert CloudAgentResource.from_delete_json(body).status is None
    assert api.stopped == ["life"]


def test_updating_chats_refuses_rather_than_lying(base: str) -> None:
    """A local agent's chat grant is its activation credential. Answering 200 to
    a write that changed nothing is the failure worth avoiding."""
    status, body = _call(
        base, "PUT", "/v1/agents/cloud/life/chats", {"chat_uids": ["cht_other"]}
    )

    assert status == 409
    assert "activate" in body["detail"]


def test_every_route_is_behind_the_bearer(base: str) -> None:
    """These routes start and stop containers; an unauthenticated one is a
    remote shell for anything that can reach the port."""
    for method, path in (
        ("GET", "/v1/agents/cloud"),
        ("GET", "/v1/agents/cloud/life"),
        ("DELETE", "/v1/agents/cloud/life"),
    ):
        status, body = _call(base, method, path, token=None)
        assert status == 401
        assert body == {"detail": "unauthorized"}

    status, _ = _call(base, "GET", "/v1/agents/cloud", token="wrong")
    assert status == 401


def test_an_unknown_path_is_404_and_a_wrong_method_is_405(base: str) -> None:
    assert _call(base, "GET", "/v1/agents")[0] == 404
    assert _call(base, "POST", "/v1/agents/cloud/life")[0] == 405
    assert _call(base, "GET", "/v1/agents/cloud/life/chats")[0] == 405


def test_serving_without_a_token_is_refused(run) -> None:
    """The bind is loopback and the token is mandatory, in that order: a
    listener that starts without one is the whole risk of this feature."""
    result = run("serve", env={"AGENT_MGR_SERVE_TOKEN": ""})

    assert result.returncode != 0
    assert "AGENT_MGR_SERVE_TOKEN is unset" in result.stderr


def test_create_refuses_a_provider_that_is_not_this_host(base: str) -> None:
    """A request naming exe must not quietly start a container here: the caller
    would believe it provisioned a tenant and never learn otherwise."""
    status, body = _call(
        base,
        "POST",
        "/v1/agents/cloud",
        {"name": "life", "provider": "exe:hermes", "chat_uids": ["cht_home"]},
    )

    assert status == 400
    assert "belongs to another provider" in body["detail"]


def test_delete_drops_the_registry_row_it_reported_deleted(base: str, api: _Api) -> None:
    """Answering with a deleted resource while leaving a row the next GET
    resolves reports a deletion that did not happen."""
    assert [entry.name for entry in api.registry.entries()] == ["life"]

    status, _ = _call(base, "DELETE", "/v1/agents/cloud/life")

    assert status == 200
    assert api.stopped == ["life"]
    assert [entry.name for entry in api.registry.entries()] == []


def test_updating_chats_to_the_set_it_already_has_is_not_a_write(
    base: str, api: _Api, monkeypatch
) -> None:
    """An idempotent caller PUTs its desired state on every reconcile; failing a
    request that asks for what is already true breaks it against a host that
    already matches."""
    monkeypatch.setattr("agent_mgr.serve._dotenv_chats", lambda agent: ("cht_home",))

    same, _ = _call(base, "PUT", "/v1/agents/cloud/life/chats", {"chat_uids": ["cht_home"]})
    other, body = _call(base, "PUT", "/v1/agents/cloud/life/chats", {"chat_uids": ["cht_x"]})

    assert same == 200
    assert other == 409
    assert "activate" in body["detail"]


def test_a_public_bind_is_refused_without_an_explicit_opt_out(run) -> None:
    """A bearer does not earn a public bind: 0.0.0.0 puts container start/stop
    in front of every device on the network, one guess away."""
    result = run("serve", "0.0.0.0", env={"AGENT_MGR_SERVE_TOKEN": "tok"})

    assert result.returncode != 0
    assert "refusing to bind 0.0.0.0" in result.stderr
