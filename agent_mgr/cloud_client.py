from __future__ import annotations

import re
from dataclasses import dataclass

from agent_mgr.cloud_http import CloudTransport
from agent_mgr.cloud_models import (
    AssistantResource,
    AssistantSlot,
    CreateAssistantRequest,
    nonempty_string,
)
from agent_mgr.errors import AgentMgrError, ErrorCode

CLOUD_PATH = "/v1/assistants"


def _assistant_uid(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, f"invalid assistant uid: {value}")
    return value


@dataclass(frozen=True, slots=True)
class CloudClient:
    transport: CloudTransport

    def create(self, request: CreateAssistantRequest) -> AssistantResource:
        value = self.transport.request("POST", CLOUD_PATH, request.to_json())
        return AssistantResource.from_json(value)

    def list(self) -> tuple[AssistantSlot, ...]:
        value = self.transport.request("GET", CLOUD_PATH)
        if not isinstance(value, list):
            raise AgentMgrError(ErrorCode.INVALID_RESPONSE, "assistant slot list is not an array")
        return tuple(AssistantSlot.from_json(item) for item in value)

    def get(self, uid: str) -> AssistantResource:
        value = self.transport.request("GET", f"{CLOUD_PATH}/{_assistant_uid(uid)}")
        return AssistantResource.from_json(value)

    def move(self, uid: str, line_uid: str) -> AssistantResource:
        """Put the assistant on another line. Its anchor chats follow the move."""
        path = f"{CLOUD_PATH}/{_assistant_uid(uid)}/line"
        value = self.transport.request(
            "PUT", path, {"line_uid": nonempty_string(line_uid, "line_uid")}
        )
        return AssistantResource.from_json(value)

    def delete(self, uid: str) -> AssistantResource:
        value = self.transport.request("DELETE", f"{CLOUD_PATH}/{_assistant_uid(uid)}")
        return AssistantResource.from_delete_json(value)
