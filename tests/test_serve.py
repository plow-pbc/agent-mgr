"""The served half of parity: Plow's wire contract, against local containers.

These drive the real handler over a real socket rather than calling the API
object directly -- the routing, the auth gate and the status codes are the
contract, and a test that skips the transport asserts none of them.
"""

from __future__ import annotations

import json
import os
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
        # The line the credential grants. A registered agent in these tests is
        # an ACTIVATED one: CREATE refuses an uncredentialed agent outright, and
        # the credential these tests would have to write lives in a real home.
        self.line: tuple[str, ...] = ("ln_home",)

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

    def _admit(self, agent) -> None:  # type: ignore[override]
        # Ownership and descriptor resolution need a real checkout; docker is
        # what these tests must not reach, and this is the seam between them.
        return

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
def api(tmp_path, monkeypatch) -> _Api:
    import agent_mgr.serve as serve_module

    registry = Registry(tmp_path / "agents")
    repo = tmp_path / "life-repo"
    repo.mkdir()
    registry.add("life", repo)
    fake = _Api(registry, {"life": _resource("life")})
    monkeypatch.setattr(serve_module, "_agent_line", lambda agent: fake.line)
    return fake


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
        {"name": "life", "provider": "local:docker", "line_uid": "ln_home"},
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
        {"name": "ghost", "provider": "local:docker", "line_uid": "ln_home"},
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


def test_updating_the_line_refuses_rather_than_lying(base: str) -> None:
    """A local agent's line comes from the credential minted for it. Answering
    200 to a write that changed nothing is the failure worth avoiding."""
    status, body = _call(base, "PUT", "/v1/agents/cloud/life/line", {"line_uid": "ln_other"})

    assert status == 409
    assert "bound to" in body["detail"]


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
    assert _call(base, "GET", "/v1/agents/cloud/life/line")[0] == 405


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
        {"name": "life", "provider": "exe:hermes", "line_uid": "ln_home"},
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


def test_updating_to_the_line_it_already_serves_is_not_a_write(
    base: str, api: _Api, monkeypatch
) -> None:
    """An idempotent caller PUTs its desired state on every reconcile; failing a
    request that asks for what is already true breaks it against a host that
    already matches."""
    monkeypatch.setattr("agent_mgr.serve._agent_line", lambda agent: ("ln_home",))

    same, _ = _call(base, "PUT", "/v1/agents/cloud/life/line", {"line_uid": "ln_home"})
    other, body = _call(base, "PUT", "/v1/agents/cloud/life/line", {"line_uid": "ln_x"})

    assert same == 200
    assert other == 409
    assert "bound to" in body["detail"]


def test_a_public_bind_is_refused_without_an_explicit_opt_out(run) -> None:
    """A bearer does not earn a public bind: 0.0.0.0 puts container start/stop
    in front of every device on the network, one guess away."""
    result = run("serve", "0.0.0.0", env={"AGENT_MGR_SERVE_TOKEN": "tok"})

    assert result.returncode != 0
    assert "refusing to bind 0.0.0.0" in result.stderr


def test_create_answers_before_the_container_is_up(base: str, api: _Api) -> None:
    """202 is the contract because provisioning outlasts a request: exe's unpack
    can outlast the gateway's sixty seconds, and a first `docker pull` here is
    minutes. Waiting made the one-click create time out in the client while the
    work was succeeding."""
    started = threading.Event()
    release = threading.Event()

    def slow(agent, argv, failure):  # noqa: ANN001, ANN202
        started.set()
        release.wait(5)
        api.started.append(agent.name)

    api._guarded = slow  # type: ignore[method-assign]

    status, body = _call(
        base,
        "POST",
        "/v1/agents/cloud",
        {"name": "life", "provider": "local:docker", "line_uid": "ln_home"},
    )

    # Answered while the bring-up is still inside `slow`.
    assert status == 202
    assert body["status"] == "provisioning"
    assert started.wait(5), "the bring-up never started"
    assert api.started == [], "the answer waited for the container"
    release.set()


def test_a_public_bind_is_refused_outright(run) -> None:
    """No escape hatch: this serves plain HTTP with a replayable bearer, so any
    non-loopback bind puts container start/stop in front of an on-path peer who
    only has to replay a request. A token does not fix a cleartext transport."""
    for host in ("0.0.0.0", "192.168.1.10"):
        result = run("serve", host, env={"AGENT_MGR_SERVE_TOKEN": "tok"})
        assert result.returncode != 0
        assert f"refusing to bind {host}" in result.stderr
        assert "replayable bearer" in result.stderr


def test_a_second_create_is_coalesced_not_doubled(base: str, api: _Api) -> None:
    """An untracked worker let a second POST start a second `up` against the
    same home -- which is the two-gateways-one-home failure this whole repo
    exists to prevent."""
    release = threading.Event()

    def slow(agent, argv, failure):  # noqa: ANN001, ANN202
        release.wait(5)
        api.started.append(agent.name)

    api._guarded = slow  # type: ignore[method-assign]
    body = {"name": "life", "provider": "local:docker", "line_uid": "ln_home"}

    first, _ = _call(base, "POST", "/v1/agents/cloud", body)
    second, payload = _call(base, "POST", "/v1/agents/cloud", body)

    assert (first, second) == (202, 202)
    assert payload["status"] == "provisioning"
    release.set()


def test_delete_refuses_while_a_start_is_in_flight(base: str, api: _Api) -> None:
    """Dropping the row mid-start leaves a running gateway nothing owns."""
    release = threading.Event()
    api._guarded = lambda agent, argv, failure: release.wait(5)  # type: ignore[method-assign]

    _call(
        base,
        "POST",
        "/v1/agents/cloud",
        {"name": "life", "provider": "local:docker", "line_uid": "ln_home"},
    )
    status, body = _call(base, "DELETE", "/v1/agents/cloud/life")
    release.set()

    assert status == 409
    assert "still provisioning" in body["detail"]
    assert [entry.name for entry in api.registry.entries()] == ["life"]


def test_a_tailnet_bind_is_allowed_and_a_lan_one_is_not(run, monkeypatch) -> None:
    """The objection is a replayable bearer on a cleartext path, and a tailnet
    is the one non-loopback case where that does not hold: WireGuard between two
    authenticated peers leaves no on-path position to replay from. A LAN or
    wildcard bind still does, so it stays refused."""
    import agent_mgr.serve as serve_module

    monkeypatch.setattr(serve_module, "_tailscale_addresses", lambda: frozenset({"100.98.135.0"}))

    from agent_mgr.errors import AgentMgrError

    # It returns the ADDRESS to bind, never the name: validating a hostname and
    # then handing the name to the socket resolves it twice, and a name carrying
    # both an owned tailnet address and a public one would bind the public one.
    assert serve_module.resolve_bind("127.0.0.1") == "127.0.0.1"
    assert serve_module.resolve_bind("100.98.135.0") == "100.98.135.0"
    for refused in ("0.0.0.0", "192.168.15.12"):
        with pytest.raises(AgentMgrError):
            serve_module.resolve_bind(refused)


def test_a_name_that_also_resolves_off_tailnet_is_refused(monkeypatch) -> None:
    """One unsafe address in the set is enough: which one the socket would pick
    is not ours to assume, so a mixed name refuses rather than gambling."""
    import agent_mgr.serve as serve_module
    from agent_mgr.errors import AgentMgrError

    monkeypatch.setattr(serve_module, "_tailscale_addresses", lambda: frozenset({"100.98.135.0"}))
    monkeypatch.setattr(
        serve_module.socket,
        "getaddrinfo",
        lambda host, port: [
            (0, 0, 0, "", ("100.98.135.0", 0)),
            (0, 0, 0, "", ("203.0.113.9", 0)),
        ],
    )

    with pytest.raises(AgentMgrError):
        serve_module.resolve_bind("mixed.example.ts.net")


def test_without_a_tailnet_only_loopback_is_allowed(monkeypatch) -> None:
    """`100.64/10` is shared CGNAT, not Tailscale's alone -- asking `tailscale`
    rather than matching the prefix is what keeps a carrier-assigned address on
    an ordinary network from passing as a tailnet one."""
    import agent_mgr.serve as serve_module

    monkeypatch.setattr(serve_module, "_tailscale_addresses", lambda: frozenset())

    from agent_mgr.errors import AgentMgrError

    assert serve_module.resolve_bind("localhost") == "localhost"
    with pytest.raises(AgentMgrError):
        serve_module.resolve_bind("100.98.135.0")


def test_an_unreadable_line_refuses_rather_than_reading_as_no_mismatch(
    base: str, api: _Api
) -> None:
    """Collapsing "not credentialed" and "the API did not answer" into the same
    empty answer made CREATE read an outage as agreement: 202 for the wrong
    line, while PUT of the right one refused. An unanswerable question refuses."""
    import agent_mgr.serve as serve_module

    def unreachable(agent):  # noqa: ANN001, ANN202
        raise serve_module.ApiError(502, "could not read the line from Plow")

    original = serve_module._agent_line
    serve_module._agent_line = unreachable  # type: ignore[assignment]
    try:
        status, body = _call(
            base,
            "POST",
            "/v1/agents/cloud",
            {"name": "life", "provider": "local:docker", "line_uid": "ln_home"},
        )
    finally:
        serve_module._agent_line = original  # type: ignore[assignment]

    assert status == 502
    assert "could not read" in body["detail"]


def test_an_uncredentialed_agent_is_refused_rather_than_promised_a_line(
    base: str, api: _Api
) -> None:
    """Nothing on this host mints a credential, so an agent that holds none
    cannot be put on the requested line. Answering 202 told the caller it had
    selected a number that was never assigned -- and holding the request in
    memory only moved the lie, since a `serve` restart turned the same resource
    into `line:unassigned`."""
    import agent_mgr.serve as serve_module

    def unknown(agent):  # noqa: ANN001, ANN202
        raise serve_module.LineUnknown

    original = serve_module._agent_line
    serve_module._agent_line = unknown  # type: ignore[assignment]
    try:
        status, _ = _call(
            base,
            "POST",
            "/v1/agents/cloud",
            {"name": "life", "provider": "local:docker", "line_uid": "ln_home"},
        )
    finally:
        serve_module._agent_line = original  # type: ignore[assignment]

    assert status == 409


def test_a_duplicate_create_does_not_deadlock(base: str, api: _Api) -> None:
    """`_provisioning()` is called holding the lock and reads the failure map,
    which takes it again. A plain Lock deadlocked the coalesced path outright,
    so the second POST never answered at all."""
    release = threading.Event()
    api._guarded = lambda agent, argv, failure: release.wait(5)  # type: ignore[method-assign]
    body = {"name": "life", "provider": "local:docker", "line_uid": "ln_home"}

    first, _ = _call(base, "POST", "/v1/agents/cloud", body)
    second, payload = _call(base, "POST", "/v1/agents/cloud", body)
    release.set()

    assert (first, second) == (202, 202)
    assert payload["status"] == "provisioning"


def test_an_uncredentialed_resource_still_parses(tmp_path, monkeypatch) -> None:
    """`CloudAgentResource` requires a non-empty grant, so answering `[]` for an
    agent whose credential has not landed produced a body Plow's own parser --
    and therefore every client -- refuses. Until the credential arrives, the
    line it was created against is its grant.

    Against the real `LocalCloudApi`, not the fake: the projection under test is
    `_resource` itself, which the fake replaces.
    """
    import agent_mgr.serve as serve_module

    registry = Registry(tmp_path / "agents")
    api = serve_module.LocalCloudApi(registry, ROOT)
    agent = SimpleNamespace(name="life", container="hermes-life", home=Path("/nowhere"))
    monkeypatch.setattr(serve_module, "_dotenv_chats", lambda a: ())
    monkeypatch.setattr(
        serve_module, "_status", lambda a: (serve_module.CloudStatus.PROVISIONING, None)
    )

    payload = api._resource(agent).to_json()

    # Unassigned is what it is -- CREATE refuses an uncredentialed agent, so no
    # resource here claims a line no credential backs.
    assert payload["chat_uids"] == ["line:unassigned"]
    CloudAgentResource.from_json(payload)


def test_an_unreadable_dotenv_is_not_read_as_uncredentialed(tmp_path, monkeypatch) -> None:
    """Collapsing a read failure into "" let POST skip line validation on a file
    it could not read and answer 202 for a line it never checked."""
    import agent_mgr.serve as serve_module
    from agent_mgr.errors import AgentMgrError, ErrorCode

    def unreadable(path, key):  # noqa: ANN001, ANN202
        raise AgentMgrError(ErrorCode.IO_ERROR, "permission denied")

    monkeypatch.setattr(serve_module, "dotenv_read", unreadable)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("PLOW_AGENT_TOKEN=x\n")
    agent = SimpleNamespace(name="life", container="hermes-life", home=home)

    with pytest.raises(serve_module.ApiError) as raised:
        serve_module._agent_line(agent)

    assert raised.value.status == 502


def test_a_recorded_failure_outranks_what_docker_reports(tmp_path, monkeypatch) -> None:
    """A pull that failed leaves no container at all, so `_status` answers
    PROVISIONING. Letting that win reported an agent that will never come up as
    still coming up, and the caller polls forever."""
    import agent_mgr.serve as serve_module

    registry = Registry(tmp_path / "agents")
    api = serve_module.LocalCloudApi(registry, ROOT)
    agent = SimpleNamespace(name="life", container="hermes-life", home=Path("/nowhere"))
    monkeypatch.setattr(serve_module, "_dotenv_chats", lambda a: ("cht_home",))
    monkeypatch.setattr(
        serve_module, "_status", lambda a: (serve_module.CloudStatus.PROVISIONING, None)
    )
    api._failed["life"] = serve_module.FailureCode.SETUP_FAILED

    payload = api._resource(agent).to_json()

    assert payload["status"] == "failed"
    assert payload["failure_code"] == "setup_failed"


def test_a_repaired_container_clears_a_recorded_failure(tmp_path, monkeypatch) -> None:
    """A container brought back by hand, or by a later successful create, must
    not stay FAILED forever because an earlier pull failed -- and identity is
    already settled inside `_status`, so clearing the record must not inspect
    the same container a second time."""
    import agent_mgr.serve as serve_module

    registry = Registry(tmp_path / "agents")
    api = serve_module.LocalCloudApi(registry, ROOT)
    agent = SimpleNamespace(name="life", container="hermes-life", home=Path("/nowhere"))
    inspected: list[object] = []
    monkeypatch.setattr(serve_module, "_dotenv_chats", lambda a: ("cht_home",))
    monkeypatch.setattr(serve_module, "_status", lambda a: (serve_module.CloudStatus.RUNNING, None))
    monkeypatch.setattr(serve_module, "require_container_ours", inspected.append)
    api._failed["life"] = serve_module.FailureCode.SETUP_FAILED

    payload = api._resource(agent).to_json()

    assert payload["status"] == "running"
    assert payload["failure_code"] is None
    assert "life" not in api._failed
    assert inspected == []


@pytest.mark.parametrize(
    ("code", "failure"),
    [("INVALID_DESCRIPTOR", "validation_failed"), ("IO_ERROR", "provider_unreachable")],
    ids=["foreign-mount", "docker-outage"],
)
@pytest.mark.parametrize("running", [True, False], ids=["running", "stopped"])
def test_a_foreign_container_and_a_docker_outage_are_different_diagnoses(
    monkeypatch, code, failure, running
) -> None:
    """A Boolean ownership check made both of these `validation_failed`, so a
    polling client told to fix its descriptor was really looking at a docker
    that could not answer. A STOPPED container needs the same question: a reused
    project mounting a sibling's home is not this agent's setup that failed."""
    import agent_mgr.serve as serve_module
    from agent_mgr.errors import AgentMgrError, ErrorCode

    agent = SimpleNamespace(name="life", container="hermes-life", home=Path("/nowhere"))
    monkeypatch.setattr(
        serve_module,
        "compose",
        lambda a, argv, capture=False: SimpleNamespace(
            returncode=0, stdout="abc123\n" if running or "--all" in argv else ""
        ),
    )

    def refuse(a):  # noqa: ANN001, ANN202
        raise AgentMgrError(ErrorCode[code], "no")

    monkeypatch.setattr(serve_module, "require_container_ours", refuse)

    status, reported = serve_module._status(agent)

    assert status is serve_module.CloudStatus.FAILED
    assert reported.value == failure


def test_a_stopped_container_that_is_ours_is_still_setup_failed(monkeypatch) -> None:
    """The ownership question must not swallow the ordinary case: our own
    container that exists and is not running is a provision that did not
    finish."""
    import agent_mgr.serve as serve_module

    agent = SimpleNamespace(name="life", container="hermes-life", home=Path("/nowhere"))
    monkeypatch.setattr(
        serve_module,
        "compose",
        lambda a, argv, capture=False: SimpleNamespace(
            returncode=0, stdout="abc123\n" if "--all" in argv else ""
        ),
    )
    monkeypatch.setattr(serve_module, "require_container_ours", lambda a: None)

    assert serve_module._status(agent) == (
        serve_module.CloudStatus.FAILED,
        serve_module.FailureCode.SETUP_FAILED,
    )


def test_a_registered_checkout_with_no_dotenv_still_reads_back(tmp_path, monkeypatch) -> None:
    """A fresh checkout that has never run `restore` has no `.env` at all.
    `_agent_line` read that as uncredentialed, but the projection went straight
    to the reader and turned the ENOENT into a failed READ -- so a registered
    agent could not even be listed until it was activated."""
    import agent_mgr.serve as serve_module

    registry = Registry(tmp_path / "agents")
    api = serve_module.LocalCloudApi(registry, ROOT)
    home = tmp_path / "home"
    home.mkdir()
    agent = SimpleNamespace(name="life", container="hermes-life", home=home)
    monkeypatch.setattr(
        serve_module, "_status", lambda a: (serve_module.CloudStatus.PROVISIONING, None)
    )
    with pytest.raises(serve_module.LineUnknown):
        serve_module._agent_line(agent)

    assert api._resource(agent).to_json()["chat_uids"] == ["line:unassigned"]


def test_the_control_bearer_does_not_reach_agent_hooks(monkeypatch) -> None:
    """`transition` runs the agent's own pre-transition hook, which inherits
    this process's environment -- so the fleet-wide control token was handed to
    agent-specific code on every create and delete."""
    import agent_mgr.serve as serve_module

    monkeypatch.setenv("AGENT_MGR_SERVE_TOKEN", "fleet-secret")
    serve_module._without_control_token()

    assert "AGENT_MGR_SERVE_TOKEN" not in os.environ
