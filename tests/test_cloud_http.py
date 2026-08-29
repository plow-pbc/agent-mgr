from __future__ import annotations

from io import BytesIO
from typing import Self
from urllib import error, request

import pytest

from agent_mgr.cloud_http import HttpCloudTransport
from agent_mgr.errors import AgentMgrError, ErrorCode


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _RecordingOpener:
    def __init__(self) -> None:
        self.requests: list[request.Request] = []
        self.handlers: tuple[object, ...] = ()
        self._response = _Response(b"null")
        self._error: Exception | None = None

    def respond(self, status: int, payload: bytes) -> None:
        assert 200 <= status < 300
        self._response = _Response(payload)
        self._error = None

    def raise_http_error(self, status: int, payload: bytes) -> None:
        self._error = error.HTTPError(
            "https://api.example/v1/agents/cloud",
            status,
            "remote error",
            None,
            BytesIO(payload),
        )

    def raise_url_error(self, reason: str) -> None:
        self._error = error.URLError(reason)

    def open(self, sent: request.Request, *, timeout: float) -> _Response:
        assert timeout == 30.0
        self.requests.append(sent)
        if self._error is not None:
            raise self._error
        return self._response


@pytest.fixture
def recording_opener(monkeypatch: pytest.MonkeyPatch) -> _RecordingOpener:
    opener = _RecordingOpener()

    def build_opener(*handlers: object) -> _RecordingOpener:
        opener.handlers = handlers
        return opener

    monkeypatch.setattr(request, "build_opener", build_opener)
    return opener


def configured_transport(recording_opener: _RecordingOpener) -> HttpCloudTransport:
    return HttpCloudTransport.from_environment(
        {
            "PLOW_API_BASE": "https://api.example/",
            "PLOW_API_TOKEN": "secret-token",
        }
    )


@pytest.mark.parametrize("missing", ["PLOW_API_BASE", "PLOW_API_TOKEN"])
def test_environment_requires_both_cloud_values(missing: str) -> None:
    environ = {
        "PLOW_API_BASE": "https://api.example",
        "PLOW_API_TOKEN": "secret-token",
    }
    del environ[missing]
    with pytest.raises(AgentMgrError) as raised:
        HttpCloudTransport.from_environment(environ)
    assert raised.value.code is ErrorCode.CONFIGURATION_ERROR
    assert "secret-token" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example",
        "ftp://api.example",
        "https://user:password@api.example",
        "https://api.example/path?query=yes",
        "https://api.example/path#fragment",
    ],
)
def test_base_url_rejects_unsafe_or_ambiguous_origins(url: str) -> None:
    with pytest.raises(AgentMgrError):
        HttpCloudTransport.from_environment(
            {"PLOW_API_BASE": url, "PLOW_API_TOKEN": "secret-token"}
        )


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000"])
def test_loopback_http_is_allowed_for_development(url: str) -> None:
    assert HttpCloudTransport.from_environment(
        {"PLOW_API_BASE": url, "PLOW_API_TOKEN": "secret-token"}
    ).base_url == url


def test_transport_repr_redacts_token() -> None:
    transport = HttpCloudTransport.from_environment(
        {"PLOW_API_BASE": "https://api.example", "PLOW_API_TOKEN": "secret-token"}
    )
    assert "secret-token" not in repr(transport)


def test_transport_sends_authenticated_compact_utf8_json(
    recording_opener: _RecordingOpener,
) -> None:
    recording_opener.respond(200, b'{"ok":true}')
    configured_transport(recording_opener).request(
        "POST", "/v1/agents/cloud", {"name": "caf\N{LATIN SMALL LETTER E WITH ACUTE}"}
    )

    [sent] = recording_opener.requests
    headers = {name.lower(): value for name, value in sent.header_items()}
    assert sent.get_method() == "POST"
    assert sent.full_url == "https://api.example/v1/agents/cloud"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert headers["authorization"] == "Bearer secret-token"
    assert sum(name.lower() == "authorization" for name, _ in sent.header_items()) == 1
    assert sent.data == b'{"name":"caf\xc3\xa9"}'


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_transport_omits_body_for_bodyless_methods(
    method: str, recording_opener: _RecordingOpener
) -> None:
    configured_transport(recording_opener).request(
        method, "/v1/agents/cloud", {"ignored": True}
    )
    [sent] = recording_opener.requests
    assert sent.data is None


def test_transport_decodes_a_json_success(recording_opener: _RecordingOpener) -> None:
    recording_opener.respond(200, b'{"agent_id":"abc"}')
    transport = configured_transport(recording_opener)
    assert transport.request("GET", "/v1/agents/cloud/abc") == {"agent_id": "abc"}


def test_transport_rejects_malformed_success_json(
    recording_opener: _RecordingOpener,
) -> None:
    recording_opener.respond(200, b"not-json-secret-token")
    with pytest.raises(AgentMgrError) as raised:
        configured_transport(recording_opener).request("GET", "/v1/agents/cloud")
    assert raised.value.code is ErrorCode.INVALID_RESPONSE
    assert "secret-token" not in str(raised.value)


def test_transport_reports_only_recognized_remote_detail(
    recording_opener: _RecordingOpener,
) -> None:
    recording_opener.raise_http_error(
        400,
        b'{"detail":"provider is not available","token":"secret-token"}',
    )
    with pytest.raises(AgentMgrError) as raised:
        configured_transport(recording_opener).request("GET", "/v1/agents/cloud")
    assert raised.value.code is ErrorCode.REMOTE_REJECTED
    assert str(raised.value) == "Plow API rejected the request (400): provider is not available"
    assert "secret-token" not in str(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"secret-token",
        b'["secret-token"]',
        b'{"detail":["secret-token"]}',
    ],
)
def test_transport_does_not_report_unrecognized_remote_bodies(
    payload: bytes, recording_opener: _RecordingOpener
) -> None:
    recording_opener.raise_http_error(400, payload)
    with pytest.raises(AgentMgrError) as raised:
        configured_transport(recording_opener).request("GET", "/v1/agents/cloud")
    assert raised.value.code is ErrorCode.REMOTE_REJECTED
    assert str(raised.value) == "Plow API rejected the request (400)"


def test_transport_sanitizes_unreachable_failures(
    recording_opener: _RecordingOpener,
) -> None:
    recording_opener.raise_url_error("upstream included secret-token")
    with pytest.raises(AgentMgrError) as raised:
        configured_transport(recording_opener).request("GET", "/v1/agents/cloud")
    assert raised.value.code is ErrorCode.REMOTE_UNREACHABLE
    assert str(raised.value) == "Plow API is unreachable"
    assert "secret-token" not in str(raised.value)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_transport_rejects_redirects_without_a_second_request(
    status: int, recording_opener: _RecordingOpener
) -> None:
    recording_opener.raise_http_error(status, b'{"detail":"moved"}')
    with pytest.raises(AgentMgrError) as raised:
        configured_transport(recording_opener).request("GET", "/v1/agents/cloud")
    assert raised.value.code is ErrorCode.REMOTE_REJECTED
    assert len(recording_opener.requests) == 1
    assert any(
        isinstance(handler, request.HTTPRedirectHandler)
        for handler in recording_opener.handlers
    )


@pytest.mark.parametrize(
    "path",
    ["https://evil.example/v1/agents/cloud", "//evil.example/v1/agents/cloud"],
)
def test_transport_rejects_paths_that_are_absolute_urls(
    path: str, recording_opener: _RecordingOpener
) -> None:
    with pytest.raises(AgentMgrError) as raised:
        configured_transport(recording_opener).request("GET", path)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert recording_opener.requests == []
