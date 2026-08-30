from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from agent_mgr.errors import AgentMgrError, ErrorCode
from agent_mgr.models import JsonValue


class CloudStatus(StrEnum):
    RUNNING = "running"
    PROVISIONING = "provisioning"
    TEARDOWN = "teardown"
    FAILED = "failed"


class FailureCode(StrEnum):
    PROVIDER_UNREACHABLE = "provider_unreachable"
    IMAGE_PULL_TIMEOUT = "image_pull_timeout"
    SETUP_FAILED = "setup_failed"
    VALIDATION_FAILED = "validation_failed"
    UNKNOWN = "unknown"


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
        if code is ErrorCode.INVALID_RESPONSE:
            _error(code, "cloud agent response contains unknown fields")
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
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _error(code, f"{field} must contain valid Unicode")
    return value


def _chat_uids(
    value: object,
    code: ErrorCode = ErrorCode.INVALID_ARGUMENT,
    *,
    deduplicate: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _error(code, "chat_uids must be an array")
    validated = [_nonempty_string(item, "chat_uids", code) for item in value]
    chat_uids = tuple(dict.fromkeys(validated)) if deduplicate else tuple(validated)
    if not chat_uids:
        _error(code, "chat_uids must not be empty")
    return chat_uids


@dataclass(frozen=True, slots=True)
class CreateCloudAgentRequest:
    """Provision an agent for one LINE.

    This modelled `{name, provider, chat_uids}` -- Plow's shape before agents
    became line-scoped. It is now `{line_uid, name?, provider?}`, matching
    `plow_schemas.api.cloud_agent`: an agent's identity is its number, so a
    frozen chat list could not name the threads that number receives tomorrow.
    A client that had already moved (Latch since #241) was answered with
    `unknown fields: line_uid` plus `missing required fields: chat_uids` --
    every create failed at this seam, in both directions.
    """

    line_uid: str
    name: str = "cloud agent"
    provider: str | None = None

    @classmethod
    def from_json(cls, value: object) -> CreateCloudAgentRequest:
        payload = _object(value, {"name", "provider", "line_uid"}, {"line_uid"})
        name = _nonempty_string(payload["name"], "name") if "name" in payload else "cloud agent"
        provider_value = payload.get("provider")
        provider = None if provider_value is None else _nonempty_string(provider_value, "provider")
        return cls(
            line_uid=_nonempty_string(payload["line_uid"], "line_uid"),
            name=name,
            provider=provider,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "provider": self.provider,
            "line_uid": self.line_uid,
        }


@dataclass(frozen=True, slots=True)
class UpdateCloudAgentLineRequest:
    """`PUT /v1/agents/cloud/{agent_id}/line` -- the successor to `/chats`."""

    line_uid: str

    @classmethod
    def from_json(cls, value: object) -> UpdateCloudAgentLineRequest:
        payload = _object(value, {"line_uid"}, {"line_uid"})
        return cls(line_uid=_nonempty_string(payload["line_uid"], "line_uid"))

    def to_json(self) -> dict[str, JsonValue]:
        return {"line_uid": self.line_uid}


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
        return cls._from_json(value, expect_deleted=False)

    @classmethod
    def from_delete_json(cls, value: object) -> CloudAgentResource:
        return cls._from_json(value, expect_deleted=True)

    @classmethod
    def _from_json(cls, value: object, *, expect_deleted: bool) -> CloudAgentResource:
        payload = _object(
            value,
            {"agent_id", "chat_uids", "url", "provider", "status", "failure_code"},
            {"agent_id", "chat_uids", "url", "provider", "status", "failure_code"},
            ErrorCode.INVALID_RESPONSE,
        )
        status_value = payload["status"]
        failure_code_value = payload["failure_code"]
        if status_value is not None and not isinstance(status_value, str):
            _error(ErrorCode.INVALID_RESPONSE, "status is not a public cloud status")
        if failure_code_value is not None and not isinstance(failure_code_value, str):
            _error(ErrorCode.INVALID_RESPONSE, "failure_code is not a public failure code")
        try:
            status = None if status_value is None else CloudStatus(status_value)
        except ValueError:
            _error(ErrorCode.INVALID_RESPONSE, "status is not a public cloud status")
        try:
            failure_code = None if failure_code_value is None else FailureCode(failure_code_value)
        except ValueError:
            _error(ErrorCode.INVALID_RESPONSE, "failure_code is not a public failure code")
        if expect_deleted and status is not None:
            _error(ErrorCode.INVALID_RESPONSE, "cloud delete response status must be null")
        if not expect_deleted and status is None:
            _error(ErrorCode.INVALID_RESPONSE, "cloud resource status must not be null")
        if status is CloudStatus.FAILED and failure_code is None:
            _error(ErrorCode.INVALID_RESPONSE, "failed resources require a failure_code")
        if status is not CloudStatus.FAILED and failure_code is not None:
            _error(ErrorCode.INVALID_RESPONSE, "only failed resources may have a failure_code")

        return cls(
            agent_id=_nonempty_string(payload["agent_id"], "agent_id", ErrorCode.INVALID_RESPONSE),
            chat_uids=_chat_uids(
                payload["chat_uids"],
                ErrorCode.INVALID_RESPONSE,
                deduplicate=False,
            ),
            url=_nonempty_string(payload["url"], "url", ErrorCode.INVALID_RESPONSE),
            provider=_nonempty_string(payload["provider"], "provider", ErrorCode.INVALID_RESPONSE),
            status=status,
            failure_code=failure_code,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "agent_id": self.agent_id,
            "chat_uids": list(self.chat_uids),
            "url": self.url,
            "provider": self.provider,
            "status": None if self.status is None else self.status.value,
            "failure_code": None if self.failure_code is None else self.failure_code.value,
        }
