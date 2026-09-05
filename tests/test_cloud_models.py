from typing import Any

import pytest
from conftest import ASSISTANT_CONTRACT

from agent_mgr.cloud_models import (
    AssistantResource,
    AssistantSlot,
    AssistantStatus,
    CreateAssistantRequest,
    FailureCode,
)
from agent_mgr.errors import AgentMgrError, ErrorCode


def resource(**overrides: object) -> dict[str, Any]:
    return ASSISTANT_CONTRACT[0] | overrides


def test_create_request_round_trips_the_api_shape() -> None:
    request = CreateAssistantRequest.from_json(
        {"name": "Mary", "provider": "exe:hermes", "line_uid": "ln_a"}
    )

    assert request.to_json() == {"name": "Mary", "provider": "exe:hermes", "line_uid": "ln_a"}


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"line_uid": ""},
        {"line_uid": 1},
        {"line_uid": "ln_a", "surprise": True},
        # The old contract addressed a create by its chats. The route takes a
        # line now, and a caller still sending chats must hear so.
        {"line_uid": "ln_a", "chat_uids": ["cht_a"]},
        ["ln_a"],
    ],
)
def test_create_request_rejects_every_non_contract_shape(value: object) -> None:
    with pytest.raises(AgentMgrError) as raised:
        CreateAssistantRequest.from_json(value)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT


def test_request_unknown_field_diagnostic_keeps_the_local_field_name() -> None:
    with pytest.raises(AgentMgrError) as raised:
        CreateAssistantRequest.from_json({"line_uid": "ln_a", "surprise": True})

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert str(raised.value) == "unknown fields: surprise"


def test_resource_preserves_chat_order_and_multiplicity() -> None:
    raw = resource(chat_uids=["cht_a", "cht_b", "cht_a"])

    decoded = AssistantResource.from_json(raw)

    assert decoded.status is AssistantStatus.RUNNING
    assert decoded.failure_code is None
    assert decoded.to_json() == raw


@pytest.mark.parametrize(
    ("status", "failure_code"),
    [
        ("running", "unknown"),
        ("failed", None),
        ("new-state", None),
        (None, None),
        (None, "setup_failed"),
        ("failed", "vendor_message"),
        ([], None),
        ({}, None),
        ("failed", []),
        ("failed", {}),
    ],
)
def test_resource_rejects_inconsistent_or_open_ended_states(
    status: object, failure_code: object
) -> None:
    with pytest.raises(AgentMgrError) as raised:
        AssistantResource.from_json(resource(status=status, failure_code=failure_code))

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    "overrides",
    [
        {"uid": ""},
        {"line": "ln_a"},
        {"line": {"provider_key": "+15550100001"}},
        {"url": ""},
        {"chat_uids": "cht_a"},
        {"chat_uids": [""]},
        {"daily_payment_cap_usd": 200},
        {"verbose_output_enabled": "yes"},
        {"surprise": True},
    ],
)
def test_resource_rejects_fields_the_contract_does_not_answer_with(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(AgentMgrError) as raised:
        AssistantResource.from_json(resource(**overrides))

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


def test_deleted_resource_rejects_live_status() -> None:
    with pytest.raises(AgentMgrError) as raised:
        AssistantResource.from_delete_json(resource(status="running"))

    assert raised.value.code is ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("taken", [True, False], ids=["taken", "free"])
def test_slot_round_trips_a_taken_and_a_free_line(taken: bool) -> None:
    raw = {"line": ASSISTANT_CONTRACT[0]["line"], "assistant": ASSISTANT_CONTRACT[0] if taken else None}

    slot = AssistantSlot.from_json(raw)

    assert (slot.assistant is not None) is taken
    assert slot.to_json() == raw


def test_resource_fixture_covers_and_round_trips_the_public_contract() -> None:
    decoded = [
        (
            AssistantResource.from_delete_json(raw)
            if raw["status"] is None
            else AssistantResource.from_json(raw)
        ).to_json()
        for raw in ASSISTANT_CONTRACT
    ]

    assert decoded == ASSISTANT_CONTRACT
    assert {raw["status"] for raw in ASSISTANT_CONTRACT} == {None} | {
        status.value for status in AssistantStatus
    }
    assert {raw["failure_code"] for raw in ASSISTANT_CONTRACT if raw["failure_code"] is not None} == {
        code.value for code in FailureCode
    }
    # The wire vocabulary, pinned to the words the API publishes rather than to
    # the fixture -- which the two assertions above hold to the same set.
    assert {status.value for status in AssistantStatus} == {
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
    local = [raw for raw in ASSISTANT_CONTRACT if raw["provider"] == "self_hosted"]
    assert local and local[0]["url"] is None and local[0]["chat_uids"] == [], (
        "a self-hosted assistant has no runtime and no anchors: keep that row decodable"
    )
