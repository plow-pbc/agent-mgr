from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from conftest import ASSISTANT_CONTRACT

from agent_mgr.cloud_client import CloudClient
from agent_mgr.errors import AgentMgrError, ErrorCode
from agent_mgr.models import JsonValue

RUNNING = ASSISTANT_CONTRACT[0]
TAKEN_SLOT = {"line": RUNNING["line"], "assistant": RUNNING}


@dataclass
class FakeTransport:
    """A transport the client can reach without a socket.

    Which method and path each operation sends is proven end to end in
    `test_cloud_cli.py`, against a real server. What is left for here is what
    that suite cannot show: a response the client must refuse, and an argument
    it must refuse before anything is sent.
    """

    response: object
    calls: list[tuple[str, str, dict[str, JsonValue] | None]] = field(default_factory=list)

    def request(self, method: str, path: str, body: dict[str, JsonValue] | None = None) -> object:
        self.calls.append((method, path, body))
        return self.response


@pytest.mark.parametrize(
    "response",
    [TAKEN_SLOT, [TAKEN_SLOT, RUNNING]],
    ids=["not an array", "an item that is not a slot"],
)
def test_list_rejects_a_response_that_is_not_slots(response: object) -> None:
    transport = FakeTransport(response)

    with pytest.raises(AgentMgrError) as raised:
        CloudClient(transport).list()

    assert raised.value.code is ErrorCode.INVALID_RESPONSE
    assert transport.calls == [("GET", "/v1/assistants", None)]


@pytest.mark.parametrize("uid", ["", "assistant/uid", "../assistant", "assistant?x=y"])
def test_assistant_uid_validation_happens_before_transport(uid: str) -> None:
    transport = FakeTransport(RUNNING)

    with pytest.raises(AgentMgrError) as raised:
        CloudClient(transport).get(uid)

    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert transport.calls == []
