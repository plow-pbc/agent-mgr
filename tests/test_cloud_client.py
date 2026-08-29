from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_mgr.cloud_client import CloudClient
from agent_mgr.cloud_models import (
    CloudAgentResource,
    CreateCloudAgentRequest,
    UpdateCloudAgentChatsRequest,
)
from agent_mgr.errors import AgentMgrError, ErrorCode
from agent_mgr.models import JsonValue


@dataclass
class FakeTransport:
    response: object
    calls: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def request(self, method: str, path: str, body: dict[str, JsonValue] | None = None) -> object:
        self.calls.append((method, path, body))
        return self.response


def resource() -> CloudAgentResource:
    return CloudAgentResource.from_json(
        {
            "agent_id": "agent-id",
            "chat_uids": ["chat-a"],
            "url": "https://agent.example",
            "provider": "exe:hermes",
            "status": "running",
            "failure_code": None,
        }
    )


def deleted_resource() -> CloudAgentResource:
    deleted = resource().to_json()
    deleted["status"] = None
    return CloudAgentResource.from_delete_json(deleted)


LIVE = resource()
DELETED = deleted_resource()
CREATE = CreateCloudAgentRequest(("chat-a",), "Mary", "exe:hermes")
UPDATE = UpdateCloudAgentChatsRequest(("chat-a", "chat-b"))


@pytest.mark.parametrize("operation", ["create", "list", "get", "update_chats", "delete"])
def test_operations_map_transport_calls(operation: str) -> None:
    cases = {
        "create": (LIVE.to_json(), (CREATE,), LIVE, ("POST", "/v1/agents/cloud", CREATE.to_json())),
        "list": ([LIVE.to_json()], (), (LIVE,), ("GET", "/v1/agents/cloud", None)),
        "get": (LIVE.to_json(), ("agent-id",), LIVE, ("GET", "/v1/agents/cloud/agent-id", None)),
        "update_chats": (
            LIVE.to_json(),
            ("agent-id", UPDATE),
            LIVE,
            ("PUT", "/v1/agents/cloud/agent-id/chats", UPDATE.to_json()),
        ),
        "delete": (
            DELETED.to_json(),
            ("agent-id",),
            DELETED,
            ("DELETE", "/v1/agents/cloud/agent-id", None),
        ),
    }
    response, args, expected, expected_call = cases[operation]
    transport = FakeTransport(response)

    assert getattr(CloudClient(transport), operation)(*args) == expected
    assert transport.calls == [expected_call]


def test_list_rejects_non_array_response() -> None:
    transport = FakeTransport({"agent_id": "agent-id"})

    with pytest.raises(AgentMgrError) as raised:
        CloudClient(transport).list()

    assert raised.value.code is ErrorCode.INVALID_RESPONSE
    assert transport.calls == [("GET", "/v1/agents/cloud", None)]


def test_list_rejects_object_items() -> None:
    transport = FakeTransport([resource().to_json(), {"agent_id": "not-a-resource"}])

    with pytest.raises(AgentMgrError) as raised:
        CloudClient(transport).list()

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("operation", "response_status"),
    [
        ("create", None),
        ("list", None),
        ("get", None),
        ("update_chats", None),
        ("delete", "running"),
    ],
)
def test_resource_status_null_is_scoped_to_delete(
    operation: str, response_status: str | None
) -> None:
    response = resource().to_json()
    response["status"] = response_status
    transport = FakeTransport([response] if operation == "list" else response)
    client = CloudClient(transport)

    with pytest.raises(AgentMgrError) as raised:
        if operation == "create":
            client.create(CreateCloudAgentRequest(("chat-a",)))
        elif operation == "list":
            client.list()
        elif operation == "get":
            client.get("agent-id")
        elif operation == "update_chats":
            client.update_chats("agent-id", UpdateCloudAgentChatsRequest(("chat-a",)))
        else:
            client.delete("agent-id")

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("agent_id", ["", "agent/id", "../agent", "agent?x=y"])
def test_agent_id_validation_happens_before_transport(agent_id: str) -> None:
    transport = FakeTransport(resource().to_json())
    client = CloudClient(transport)

    with pytest.raises(AgentMgrError) as raised:
        client.get(agent_id)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert transport.calls == []
