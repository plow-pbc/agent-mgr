from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib import error, request
from urllib.parse import urlsplit

from agent_mgr.errors import AgentMgrError, ErrorCode
from agent_mgr.models import JsonValue

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class CloudTransport(Protocol):
    def request(
        self, method: str, path: str, body: dict[str, JsonValue] | None = None
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class HttpCloudTransport:
    base_url: str
    token: str = field(repr=False)
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> HttpCloudTransport:
        base_url = environ.get("PLOW_API_BASE", "").strip()
        token = environ.get("PLOW_API_TOKEN", "").strip()
        if not base_url:
            raise AgentMgrError(
                ErrorCode.CONFIGURATION_ERROR,
                "missing required environment variable: PLOW_API_BASE",
            )
        if not token:
            raise AgentMgrError(
                ErrorCode.CONFIGURATION_ERROR,
                "missing required environment variable: PLOW_API_TOKEN",
            )

        try:
            parsed = urlsplit(base_url)
            hostname, _ = parsed.hostname, parsed.port
        except ValueError:
            raise AgentMgrError(
                ErrorCode.CONFIGURATION_ERROR, "PLOW_API_BASE must be a valid URL origin"
            ) from None

        root_only = parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        no_userinfo = parsed.username is None and parsed.password is None
        valid_scheme = parsed.scheme == "https" or (
            parsed.scheme == "http" and hostname in _LOOPBACK_HOSTS
        )
        if not hostname or not root_only or not no_userinfo or not valid_scheme:
            raise AgentMgrError(
                ErrorCode.CONFIGURATION_ERROR,
                "PLOW_API_BASE must be a root-only HTTPS origin or loopback HTTP origin",
            )

        return cls(base_url=base_url.removesuffix("/"), token=token)

    def request(
        self, method: str, path: str, body: dict[str, JsonValue] | None = None
    ) -> object:
        if not path.startswith("/v1/agents/cloud") or urlsplit(path).netloc:
            raise AgentMgrError(
                ErrorCode.INVALID_ARGUMENT,
                "cloud API path must start with /v1/agents/cloud and be relative",
            )

        encoded_body = None
        if body is not None and method not in {"GET", "DELETE"}:
            encoded_body = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        sent = request.Request(
            f"{self.base_url}{path}",
            data=encoded_body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        opener = request.build_opener(_NoRedirect())
        try:
            with opener.open(sent, timeout=self.timeout_seconds) as response:
                payload = response.read()
        except error.HTTPError as http_error:
            message = f"Plow API rejected the request ({http_error.code})"
            detail = _remote_detail(http_error)
            if detail is not None:
                message = f"{message}: {detail.replace(self.token, '[redacted]')}"
            raise AgentMgrError(ErrorCode.REMOTE_REJECTED, message) from None
        except error.URLError:
            raise AgentMgrError(
                ErrorCode.REMOTE_UNREACHABLE, "Plow API is unreachable"
            ) from None

        try:
            decoded: object = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AgentMgrError(
                ErrorCode.INVALID_RESPONSE, "Plow API returned invalid JSON"
            ) from None
        return decoded


def _remote_detail(http_error: error.HTTPError) -> str | None:
    try:
        payload: object = json.loads(http_error.read())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    return detail if isinstance(detail, str) else None
