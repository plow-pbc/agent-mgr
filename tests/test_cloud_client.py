from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_mgr.cloud_client import CloudClient
from agent_mgr.cloud_models import AssistantResource, AssistantSlot, CreateAssistantRequest
from agent_mgr.errors import AgentMgrError, ErrorCode
from agent_mgr.models import JsonValue

CONTRACT: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "assistant-contract.json").read_text(encoding="utf-8")
)
RUNNING = CONTRACT[0]
DELETED = CONTRACT[3]
UID = RUNNING["uid"]
TAKEN_SLOT = {"line": RUNNING["line"], "assistant": RUNNING}
CREATE = CreateAssistantRequest("ln_running", "Mary", "exe:hermes")


@dataclass
class FakeTransport:
    response: object
    calls: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def request(self, method: str, path: str, body: dict[str, JsonValue] | None = None) -> object:
        self.calls.append((method, path, body))
        return self.response


@pytest.mark.parametrize("operation", ["create", "list", "get", "move", "delete"])
def test_operations_map_transport_calls(operation: str) -> None:
    live = AssistantResource.from_json(RUNNING)
    cases: dict[str, tuple[object, tuple[object, ...], object, object]] = {
        "create": (RUNNING, (CREATE,), live, ("POST", "/v1/assistants", CREATE.to_json())),
        "list": (
            [TAKEN_SLOT],
            (),
            (AssistantSlot.from_json(TAKEN_SLOT),),
            ("GET", "/v1/assistants", None),
        ),
        "get": (RUNNING, (UID,), live, ("GET", f"/v1/assistants/{UID}", None)),
        "move": (
            RUNNING,
            (UID, "ln_target"),
            live,
            ("PUT", f"/v1/assistants/{UID}/line", {"line_uid": "ln_target"}),
        ),
        "delete": (
            DELETED,
            (UID,),
            AssistantResource.from_delete_json(DELETED),
            ("DELETE", f"/v1/assistants/{UID}", None),
        ),
    }
    response, args, expected, expected_call = cases[operation]
    transport = FakeTransport(response)

    assert getattr(CloudClient(transport), operation)(*args) == expected
    assert transport.calls == [expected_call]


def test_list_rejects_non_array_response() -> None:
    transport = FakeTransport(TAKEN_SLOT)

    with pytest.raises(AgentMgrError) as raised:
        CloudClient(transport).list()

    assert raised.value.code is ErrorCode.INVALID_RESPONSE
    assert transport.calls == [("GET", "/v1/assistants", None)]


def test_list_rejects_items_that_are_not_slots() -> None:
    transport = FakeTransport([TAKEN_SLOT, RUNNING])

    with pytest.raises(AgentMgrError) as raised:
        CloudClient(transport).list()

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("uid", ["", "assistant/uid", "../assistant", "assistant?x=y"])
def test_assistant_uid_validation_happens_before_transport(uid: str) -> None:
    transport = FakeTransport(RUNNING)
    client = CloudClient(transport)

    with pytest.raises(AgentMgrError) as raised:
        client.get(uid)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert transport.calls == []
