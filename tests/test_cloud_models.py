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
    request = UpdateCloudAgentChatsRequest.from_json(
        {"chat_uids": ["cht_b", "cht_a", "cht_b"]}
    )

    assert request.to_json() == {"chat_uids": ["cht_b", "cht_a"]}


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


@pytest.mark.parametrize(
    ("status", "failure_code"),
    [
        ("running", None),
        ("provisioning", None),
        ("teardown", None),
        (None, None),
        ("failed", "provider_unreachable"),
        ("failed", "image_pull_timeout"),
        ("failed", "setup_failed"),
        ("failed", "validation_failed"),
        ("failed", "unknown"),
    ],
)
def test_resource_accepts_every_public_terminal_shape(status, failure_code) -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": status,
        "failure_code": failure_code,
    }

    assert CloudAgentResource.from_json(raw).to_json() == raw


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
    assert [CloudAgentResource.from_json(resource).to_json() for resource in resources] == resources
    assert {resource["status"] for resource in resources} == {
        None,
        "running",
        "provisioning",
        "teardown",
        "failed",
    }
