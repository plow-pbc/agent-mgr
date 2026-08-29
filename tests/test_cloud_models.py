import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_mgr.cloud_models import (
    CloudAgentResource,
    CreateCloudAgentRequest,
    UpdateCloudAgentChatsRequest,
)
from agent_mgr.errors import AgentMgrError, ErrorCode


def test_create_request_round_trips_the_api_shape() -> None:
    request = CreateCloudAgentRequest.from_json(
        {"name": "Mary", "provider": "exe:hermes", "chat_uids": ["cht_a", "cht_b"]}
    )

    assert request.to_json() == {
        "name": "Mary",
        "provider": "exe:hermes",
        "chat_uids": ["cht_a", "cht_b"],
    }


def test_update_request_deduplicates_chats_in_first_seen_order() -> None:
    request = UpdateCloudAgentChatsRequest.from_json({"chat_uids": ["cht_b", "cht_a", "cht_b"]})

    assert request.to_json() == {"chat_uids": ["cht_b", "cht_a"]}


def test_resource_preserves_chat_order_and_multiplicity() -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a", "cht_b", "cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": "running",
        "failure_code": None,
    }

    assert CloudAgentResource.from_json(raw).to_json() == raw


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"chat_uids": []},
        {"chat_uids": [""]},
        {"chat_uids": [1]},
        {"chat_uids": ["cht_a"], "surprise": True},
        ["cht_a"],
    ],
)
def test_update_request_rejects_every_non_contract_shape(value: object) -> None:
    with pytest.raises(AgentMgrError) as raised:
        UpdateCloudAgentChatsRequest.from_json(value)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_request_unknown_field_diagnostic_keeps_the_local_field_name() -> None:
    with pytest.raises(AgentMgrError) as raised:
        UpdateCloudAgentChatsRequest.from_json({"chat_uids": ["cht_a"], "surprise": True})

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert str(raised.value) == "unknown fields: surprise"


@pytest.mark.parametrize(
    ("status", "failure_code"),
    [
        ("running", "unknown"),
        ("failed", None),
        ("new-state", None),
        (None, "setup_failed"),
        ("failed", "vendor_message"),
        ([], None),
        ({}, None),
        ("failed", []),
        ("failed", {}),
    ],
)
def test_resource_rejects_inconsistent_or_open_ended_states(status, failure_code) -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": status,
        "failure_code": failure_code,
    }

    with pytest.raises(AgentMgrError) as raised:
        CloudAgentResource.from_json(raw)

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


def test_resource_fixture_covers_and_round_trips_the_public_contract() -> None:
    fixture = Path(__file__).parent / "fixtures" / "cloud-agent-contract.json"
    resources = json.loads(fixture.read_text())

    assert isinstance(resources, list)
    decoded = [
        (
            CloudAgentResource.from_delete_json(resource)
            if resource["status"] is None
            else CloudAgentResource.from_json(resource)
        ).to_json()
        for resource in resources
    ]
    assert decoded == resources
    assert {resource["status"] for resource in resources} == {
        None,
        "running",
        "provisioning",
        "teardown",
        "failed",
    }
    assert {
        resource["failure_code"] for resource in resources if resource["failure_code"] is not None
    } == {
        "provider_unreachable",
        "image_pull_timeout",
        "setup_failed",
        "validation_failed",
        "unknown",
    }
