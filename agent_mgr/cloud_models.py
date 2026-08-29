from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NoReturn, TypeAlias, cast

from agent_mgr.errors import AgentMgrError, ErrorCode
from agent_mgr.models import JsonValue

CloudStatus: TypeAlias = Literal["running", "provisioning", "teardown", "failed"]
FailureCode: TypeAlias = Literal[
    "provider_unreachable",
    "image_pull_timeout",
    "setup_failed",
    "validation_failed",
    "unknown",
]


def _error(code: ErrorCode, message: str) -> NoReturn:
    raise AgentMgrError(code, message)


def _object(
    value: object,
    allowed: set[str],
    required: set[str],
    code: ErrorCode = ErrorCode.INVALID_ARGUMENT,
) -> dict[object, object]:
    if not isinstance(value, dict):
        _error(code, "expected an object")

    unknown = value.keys() - allowed
    missing = required - value.keys()
    if unknown:
        _error(code, f"unknown fields: {', '.join(sorted(map(str, unknown)))}")
    if missing:
        _error(code, f"missing required fields: {', '.join(sorted(missing))}")
    return value


def _nonempty_string(
    value: object,
    field: str,
    code: ErrorCode = ErrorCode.INVALID_ARGUMENT,
) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        _error(code, f"{field} must be a non-empty string")
    return value


def _chat_uids(
    value: object, code: ErrorCode = ErrorCode.INVALID_ARGUMENT
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _error(code, "chat_uids must be an array")
    validated = [_nonempty_string(item, "chat_uids", code) for item in value]
    chat_uids = tuple(dict.fromkeys(validated))
    if not chat_uids:
        _error(code, "chat_uids must not be empty")
    return chat_uids


@dataclass(frozen=True, slots=True)
class CreateCloudAgentRequest:
    chat_uids: tuple[str, ...]
    name: str = "cloud agent"
    provider: str | None = None

    @classmethod
    def from_json(cls, value: object) -> CreateCloudAgentRequest:
        payload = _object(value, {"name", "provider", "chat_uids"}, {"chat_uids"})
        name = (
            _nonempty_string(payload["name"], "name")
            if "name" in payload
            else "cloud agent"
        )
        provider_value = payload.get("provider")
        provider = (
            None
            if provider_value is None
            else _nonempty_string(provider_value, "provider")
        )
        return cls(chat_uids=_chat_uids(payload["chat_uids"]), name=name, provider=provider)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "provider": self.provider,
            "chat_uids": list(self.chat_uids),
        }


@dataclass(frozen=True, slots=True)
class UpdateCloudAgentChatsRequest:
    chat_uids: tuple[str, ...]

    @classmethod
    def from_json(cls, value: object) -> UpdateCloudAgentChatsRequest:
        payload = _object(value, {"chat_uids"}, {"chat_uids"})
        return cls(chat_uids=_chat_uids(payload["chat_uids"]))

    def to_json(self) -> dict[str, JsonValue]:
        return {"chat_uids": list(self.chat_uids)}


@dataclass(frozen=True, slots=True)
class CloudAgentResource:
    agent_id: str
    chat_uids: tuple[str, ...]
    url: str
    provider: str
    status: CloudStatus | None
    failure_code: FailureCode | None

    @classmethod
    def from_json(cls, value: object) -> CloudAgentResource:
        payload = _object(
            value,
            {"agent_id", "chat_uids", "url", "provider", "status", "failure_code"},
            {"agent_id", "chat_uids", "url", "provider", "status", "failure_code"},
            ErrorCode.INVALID_RESPONSE,
        )
        status_value = payload["status"]
        failure_code_value = payload["failure_code"]
        if status_value is not None and not isinstance(status_value, str):
            _error(ErrorCode.INVALID_RESPONSE, "status must be a string or null")
        if failure_code_value is not None and not isinstance(failure_code_value, str):
            _error(ErrorCode.INVALID_RESPONSE, "failure_code must be a string or null")
        if status_value not in {"running", "provisioning", "teardown", "failed", None}:
            _error(ErrorCode.INVALID_RESPONSE, "status is not a public cloud status")
        if failure_code_value not in {
            "provider_unreachable",
            "image_pull_timeout",
            "setup_failed",
            "validation_failed",
            "unknown",
            None,
        }:
            _error(ErrorCode.INVALID_RESPONSE, "failure_code is not a public failure code")

        status = cast(CloudStatus | None, status_value)
        failure_code = cast(FailureCode | None, failure_code_value)
        if status == "failed" and failure_code is None:
            _error(ErrorCode.INVALID_RESPONSE, "failed resources require a failure_code")
        if status != "failed" and failure_code is not None:
            _error(ErrorCode.INVALID_RESPONSE, "only failed resources may have a failure_code")

        return cls(
            agent_id=_nonempty_string(
                payload["agent_id"], "agent_id", ErrorCode.INVALID_RESPONSE
            ),
            chat_uids=_chat_uids(payload["chat_uids"], ErrorCode.INVALID_RESPONSE),
            url=_nonempty_string(payload["url"], "url", ErrorCode.INVALID_RESPONSE),
            provider=_nonempty_string(
                payload["provider"], "provider", ErrorCode.INVALID_RESPONSE
            ),
            status=status,
            failure_code=failure_code,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "agent_id": self.agent_id,
            "chat_uids": list(self.chat_uids),
            "url": self.url,
            "provider": self.provider,
            "status": self.status,
            "failure_code": self.failure_code,
        }
