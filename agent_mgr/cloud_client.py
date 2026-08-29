from __future__ import annotations

import re
from dataclasses import dataclass

from agent_mgr.cloud_http import CloudTransport
from agent_mgr.cloud_models import (
    CloudAgentResource,
    CreateCloudAgentRequest,
    UpdateCloudAgentChatsRequest,
)
from agent_mgr.errors import AgentMgrError, ErrorCode

CLOUD_PATH = "/v1/agents/cloud"


def _agent_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AgentMgrError(ErrorCode.INVALID_ARGUMENT, f"invalid cloud agent id: {value}")
    return value


@dataclass(frozen=True, slots=True)
class CloudClient:
    transport: CloudTransport

    def create(self, request: CreateCloudAgentRequest) -> CloudAgentResource:
        value = self.transport.request("POST", CLOUD_PATH, request.to_json())
        return CloudAgentResource.from_json(value)

    def list(self) -> tuple[CloudAgentResource, ...]:
        value = self.transport.request("GET", CLOUD_PATH)
        if not isinstance(value, list):
            raise AgentMgrError(ErrorCode.INVALID_RESPONSE, "cloud agent list is not an array")
        return tuple(CloudAgentResource.from_json(item) for item in value)

    def get(self, agent_id: str) -> CloudAgentResource:
        value = self.transport.request("GET", f"{CLOUD_PATH}/{_agent_id(agent_id)}")
        return CloudAgentResource.from_json(value)

    def update_chats(
        self, agent_id: str, request: UpdateCloudAgentChatsRequest
    ) -> CloudAgentResource:
        path = f"{CLOUD_PATH}/{_agent_id(agent_id)}/chats"
        value = self.transport.request("PUT", path, request.to_json())
        return CloudAgentResource.from_json(value)

    def delete(self, agent_id: str) -> CloudAgentResource:
        value = self.transport.request("DELETE", f"{CLOUD_PATH}/{_agent_id(agent_id)}")
        return CloudAgentResource.from_json(value)
