from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from agent_mgr.errors import AgentMgrError, ErrorCode
from agent_mgr.models import JsonValue


class AssistantStatus(StrEnum):
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


# Every field an assistant carries on the wire. Allowed and required are the
# same set: the API answers all of them on every route, so a response missing
# one -- or carrying one more -- is contract drift this build must not read past.
ASSISTANT_FIELDS = {
    "uid",
    "provider",
    "line",
    "chat_uids",
    "url",
    "image",
    "status",
    "failure_code",
    "daily_payment_cap_usd",
    "verbose_output_enabled",
}


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
            _error(code, "assistant response contains unknown fields")
        _error(code, f"unknown fields: {', '.join(sorted(map(str, unknown)))}")
    if missing:
        _error(code, f"missing required fields: {', '.join(sorted(missing))}")
    return value


def nonempty_string(
    value: object,
    field: str,
    code: ErrorCode = ErrorCode.INVALID_ARGUMENT,
) -> str:
    """The gate every string this tool sends or reads passes through.

    Public because the client validates its `line_uid` argument with it: argv
    carries whatever bytes the shell gave it, and a value the transport cannot
    encode has to fail as this tool's JSON error rather than as an unhandled
    `UnicodeEncodeError`.
    """
    if isinstance(value, bool) or not isinstance(value, str) or not value:
        _error(code, f"{field} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _error(code, f"{field} must contain valid Unicode")
    return value


def _optional_string(value: object, field: str) -> str | None:
    """A response field the API answers with a string or an explicit null.

    A self-hosted assistant has no cloud runtime and no image, and an uncapped
    one has no daily cap -- all three are null rather than absent.
    """
    return None if value is None else nonempty_string(value, field, ErrorCode.INVALID_RESPONSE)


def _chat_uids(value: object) -> tuple[str, ...]:
    """The assistant's lifecycle-anchor chats, in the order the API gave them.

    Empty for a self-hosted assistant: nothing external was provisioned, so
    there is nothing anchored for a teardown to find.
    """
    if not isinstance(value, list):
        _error(ErrorCode.INVALID_RESPONSE, "chat_uids must be an array")
    return tuple(nonempty_string(item, "chat_uids", ErrorCode.INVALID_RESPONSE) for item in value)


def _line(value: object) -> dict[str, JsonValue]:
    """The line an assistant answers on -- forwarded, not consumed.

    `uid` is the only field agent-mgr reads; the rest travels exactly as the API
    sent it, so a field added to the line resource is not drift this tool has to
    have an opinion about.
    """
    if not isinstance(value, dict):
        _error(ErrorCode.INVALID_RESPONSE, "line must be an object")
    nonempty_string(value.get("uid"), "line uid", ErrorCode.INVALID_RESPONSE)
    return dict(value)


@dataclass(frozen=True, slots=True)
class CreateAssistantRequest:
    """Provision one assistant on one line. The slot is 1:1 with the line."""

    line_uid: str
    name: str = "cloud agent"
    provider: str | None = None

    @classmethod
    def from_json(cls, value: object) -> CreateAssistantRequest:
        payload = _object(value, {"name", "provider", "line_uid"}, {"line_uid"})
        name = nonempty_string(payload["name"], "name") if "name" in payload else "cloud agent"
        provider_value = payload.get("provider")
        provider = None if provider_value is None else nonempty_string(provider_value, "provider")
        return cls(
            line_uid=nonempty_string(payload["line_uid"], "line_uid"),
            name=name,
            provider=provider,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {"name": self.name, "provider": self.provider, "line_uid": self.line_uid}


@dataclass(frozen=True, slots=True)
class AssistantResource:
    uid: str
    line: dict[str, JsonValue]
    chat_uids: tuple[str, ...]
    url: str | None
    image: str | None
    provider: str
    status: AssistantStatus | None
    failure_code: FailureCode | None
    # A decimal string on the wire, or null for uncapped. Carried verbatim:
    # agent-mgr shows the owner's ceiling and never does arithmetic on it.
    daily_payment_cap_usd: str | None
    verbose_output_enabled: bool

    @classmethod
    def from_json(cls, value: object) -> AssistantResource:
        return cls._from_json(value, expect_deleted=False)

    @classmethod
    def from_delete_json(cls, value: object) -> AssistantResource:
        return cls._from_json(value, expect_deleted=True)

    @classmethod
    def _from_json(cls, value: object, *, expect_deleted: bool) -> AssistantResource:
        payload = _object(value, ASSISTANT_FIELDS, ASSISTANT_FIELDS, ErrorCode.INVALID_RESPONSE)
        status_value = payload["status"]
        failure_code_value = payload["failure_code"]
        verbose = payload["verbose_output_enabled"]
        if status_value is not None and not isinstance(status_value, str):
            _error(ErrorCode.INVALID_RESPONSE, "status is not a public assistant status")
        if failure_code_value is not None and not isinstance(failure_code_value, str):
            _error(ErrorCode.INVALID_RESPONSE, "failure_code is not a public failure code")
        if not isinstance(verbose, bool):
            _error(ErrorCode.INVALID_RESPONSE, "verbose_output_enabled must be a boolean")
        try:
            status = None if status_value is None else AssistantStatus(status_value)
        except ValueError:
            _error(ErrorCode.INVALID_RESPONSE, "status is not a public assistant status")
        try:
            failure_code = None if failure_code_value is None else FailureCode(failure_code_value)
        except ValueError:
            _error(ErrorCode.INVALID_RESPONSE, "failure_code is not a public failure code")
        if expect_deleted and status is not None:
            _error(ErrorCode.INVALID_RESPONSE, "delete response status must be null")
        if not expect_deleted and status is None:
            _error(ErrorCode.INVALID_RESPONSE, "assistant status must not be null")
        if status is AssistantStatus.FAILED and failure_code is None:
            _error(ErrorCode.INVALID_RESPONSE, "failed assistants require a failure_code")
        if status is not AssistantStatus.FAILED and failure_code is not None:
            _error(ErrorCode.INVALID_RESPONSE, "only failed assistants may have a failure_code")

        return cls(
            uid=nonempty_string(payload["uid"], "uid", ErrorCode.INVALID_RESPONSE),
            line=_line(payload["line"]),
            chat_uids=_chat_uids(payload["chat_uids"]),
            url=_optional_string(payload["url"], "url"),
            image=_optional_string(payload["image"], "image"),
            provider=nonempty_string(payload["provider"], "provider", ErrorCode.INVALID_RESPONSE),
            status=status,
            failure_code=failure_code,
            daily_payment_cap_usd=_optional_string(
                payload["daily_payment_cap_usd"], "daily_payment_cap_usd"
            ),
            verbose_output_enabled=verbose,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "uid": self.uid,
            "line": dict(self.line),
            "chat_uids": list(self.chat_uids),
            "url": self.url,
            "image": self.image,
            "provider": self.provider,
            "status": None if self.status is None else self.status.value,
            "failure_code": None if self.failure_code is None else self.failure_code.value,
            "daily_payment_cap_usd": self.daily_payment_cap_usd,
            "verbose_output_enabled": self.verbose_output_enabled,
        }


@dataclass(frozen=True, slots=True)
class AssistantSlot:
    """One line in the pool and the caller's assistant on it, or null when free.

    The listing answers a slot per line rather than a row per assistant, so an
    empty line is as much of an answer as a taken one.
    """

    line: dict[str, JsonValue]
    assistant: AssistantResource | None

    @classmethod
    def from_json(cls, value: object) -> AssistantSlot:
        fields = {"line", "assistant"}
        payload = _object(value, fields, fields, ErrorCode.INVALID_RESPONSE)
        assistant = payload["assistant"]
        return cls(
            line=_line(payload["line"]),
            assistant=None if assistant is None else AssistantResource.from_json(assistant),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "line": dict(self.line),
            "assistant": None if self.assistant is None else self.assistant.to_json(),
        }
