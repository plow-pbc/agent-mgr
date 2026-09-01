from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    AGENT_NOT_FOUND = "agent_not_found"
    CONFIGURATION_ERROR = "configuration_error"
    INVALID_ARGUMENT = "invalid_argument"
    INVALID_DESCRIPTOR = "invalid_descriptor"
    INVALID_NAME = "invalid_name"
    INVALID_RESPONSE = "invalid_response"
    IO_ERROR = "io_error"
    OPERATION_FAILED = "operation_failed"
    REMOTE_REJECTED = "remote_rejected"
    REMOTE_UNREACHABLE = "remote_unreachable"


@dataclass(slots=True)
class AgentMgrError(Exception):
    code: ErrorCode
    message: str
    remediation: str = ""
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message
