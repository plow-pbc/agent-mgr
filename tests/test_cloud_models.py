import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_mgr.cloud_models import (
    CloudAgentResource,
    CloudStatus,
    CreateCloudAgentRequest,
    FailureCode,
    UpdateCloudAgentLineRequest,
)
from agent_mgr.errors import AgentMgrError, ErrorCode


def test_create_request_round_trips_the_api_shape() -> None:
    """`{line_uid, name?, provider?}` -- an agent is provisioned for a LINE.

    This modelled `chat_uids` and rejected `line_uid` as unknown, so every
    create from a client that had already moved failed at this seam.
    """
    request = CreateCloudAgentRequest.from_json(
        {"name": "Mary", "provider": "exe:hermes", "line_uid": "ln_p3"}
    )

    assert request.to_json() == {
        "name": "Mary",
        "provider": "exe:hermes",
        "line_uid": "ln_p3",
    }


def test_create_request_defaults_the_name_and_provider() -> None:
    """Both are optional in Plow's schema: the caller may name only the line,
    and refusing that request would fail a legal one."""
    request = CreateCloudAgentRequest.from_json({"line_uid": "ln_p3"})

    assert request.to_json() == {"name": "cloud agent", "provider": None, "line_uid": "ln_p3"}


def test_update_request_carries_the_line() -> None:
    request = UpdateCloudAgentLineRequest.from_json({"line_uid": "ln_p4"})

    assert request.to_json() == {"line_uid": "ln_p4"}


def test_resource_preserves_chat_order_and_multiplicity() -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a", "cht_b", "cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": "running",
        "failure_code": None,
    }

    resource = CloudAgentResource.from_json(raw)

    assert resource.status is CloudStatus.RUNNING
    assert resource.failure_code is None
    assert resource.to_json() == raw


@pytest.mark.parametrize(
    "value",
    [
        {},
        {},
        {"line_uid": ""},
        {"line_uid": 1},
        {"line_uid": "ln_p3", "surprise": True},
        ["cht_a"],
    ],
)
def test_update_request_rejects_every_non_contract_shape(value: object) -> None:
    with pytest.raises(AgentMgrError) as raised:
        UpdateCloudAgentLineRequest.from_json(value)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_request_unknown_field_diagnostic_keeps_the_local_field_name() -> None:
    with pytest.raises(AgentMgrError) as raised:
        UpdateCloudAgentLineRequest.from_json({"line_uid": "ln_p3", "surprise": True})

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


def test_live_resource_rejects_null_status() -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": None,
        "failure_code": None,
    }

    with pytest.raises(AgentMgrError) as raised:
        CloudAgentResource.from_json(raw)

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


def test_deleted_resource_rejects_live_status() -> None:
    raw = {
        "agent_id": "a" * 32,
        "chat_uids": ["cht_a"],
        "url": "https://agent.example",
        "provider": "exe:hermes",
        "status": "running",
        "failure_code": None,
    }

    with pytest.raises(AgentMgrError) as raised:
        CloudAgentResource.from_delete_json(raw)

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
    assert {status.value for status in CloudStatus} == {
        "running",
        "provisioning",
        "teardown",
        "failed",
    }
    assert {code.value for code in FailureCode} == {
        "provider_unreachable",
        "image_pull_timeout",
        "setup_failed",
        "validation_failed",
        "unknown",
    }
