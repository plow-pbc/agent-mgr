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


def test_create_maps_request_and_decodes_resource() -> None:
    transport = FakeTransport(resource().to_json())
    client = CloudClient(transport)
    request = CreateCloudAgentRequest(("chat-a",), "Mary", "exe:hermes")

    result = client.create(request)

    assert isinstance(result, CloudAgentResource)
    assert transport.calls == [("POST", "/v1/agents/cloud", request.to_json())]


def test_list_maps_path_and_decodes_resources() -> None:
    expected = resource()
    transport = FakeTransport([expected.to_json()])

    result = CloudClient(transport).list()

    assert result == (expected,)
    assert isinstance(result, tuple)
    assert transport.calls == [("GET", "/v1/agents/cloud", None)]


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


def test_get_maps_agent_path_and_decodes_resource() -> None:
    transport = FakeTransport(resource().to_json())

    result = CloudClient(transport).get("agent-id")

    assert isinstance(result, CloudAgentResource)
    assert result == resource()
    assert transport.calls == [("GET", "/v1/agents/cloud/agent-id", None)]


def test_update_chats_maps_request_and_decodes_resource() -> None:
    transport = FakeTransport(resource().to_json())
    request = UpdateCloudAgentChatsRequest(("chat-a", "chat-b"))

    result = CloudClient(transport).update_chats("agent-id", request)

    assert isinstance(result, CloudAgentResource)
    assert result == resource()
    assert transport.calls == [("PUT", "/v1/agents/cloud/agent-id/chats", request.to_json())]


def test_delete_maps_agent_path_and_decodes_deleted_resource() -> None:
    deleted = resource().to_json()
    deleted["status"] = None
    transport = FakeTransport(deleted)

    result = CloudClient(transport).delete("agent-id")

    assert isinstance(result, CloudAgentResource)
    assert result.status is None
    assert transport.calls == [("DELETE", "/v1/agents/cloud/agent-id", None)]


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
