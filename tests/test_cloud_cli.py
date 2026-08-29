from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT

TOKEN = "test-token"


def _contract_resources() -> list[dict[str, Any]]:
    fixture = Path(__file__).parent / "fixtures" / "cloud-agent-contract.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


class _CloudServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object | None]] = []
        self._responses: list[tuple[int, bytes]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                assert self.headers.get("Authorization") == f"Bearer {TOKEN}"
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else None
                owner.requests.append((self.command, self.path, body))
                status, payload = owner._responses.pop(0)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            do_DELETE = _handle
            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle

            def log_message(self, format: str, *args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    @property
    def environment(self) -> dict[str, str]:
        return {
            "PLOW_API_BASE": f"http://127.0.0.1:{self.server.server_port}",
            "PLOW_API_TOKEN": TOKEN,
        }

    def respond(self, value: object, status: int = 200) -> None:
        self._responses.append((status, json.dumps(value).encode()))

    def respond_bytes(self, payload: bytes, status: int = 200) -> None:
        self._responses.append((status, payload))

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


@pytest.fixture
def cloud_server() -> Iterator[_CloudServer]:
    server = _CloudServer()
    server.start()
    try:
        yield server
    finally:
        server.close()


def _json_document(result, operation: str) -> dict[str, Any]:
    stdout = result.stdout if hasattr(result, "stdout") else result.out
    stderr = result.stderr if hasattr(result, "stderr") else result.err
    assert stderr == ""
    assert TOKEN not in stdout
    assert TOKEN not in stderr
    body = json.loads(stdout)
    assert body["schema_version"] == 1
    assert body["operation"] == operation
    return body


@pytest.mark.parametrize(
    "operation,args",
    [
        ("cloud-create", ()),
        ("cloud-list", ()),
        ("cloud-get", ("agent-id",)),
        ("cloud-update-chats", ("agent-id",)),
        ("cloud-delete", ("agent-id",)),
    ],
)
def test_cloud_commands_require_json(operation: str, args: tuple[str, ...], run) -> None:
    result = run(operation, *args)
    assert result.returncode == 2
    assert "requires --json" in result.stderr


def test_cloud_create_reads_the_api_request_shape_from_stdin(run, cloud_server) -> None:
    resource = _contract_resources()[1]
    cloud_server.respond(resource)
    result = run(
        "--json",
        "cloud-create",
        env=cloud_server.environment,
        input=json.dumps({"name": "Mary", "provider": "exe:hermes", "chat_uids": ["cht_a"]}),
    )

    assert result.returncode == 0
    body = _json_document(result, "cloud-create")
    assert body["result"]["agent"] == resource
    assert cloud_server.requests == [
        (
            "POST",
            "/v1/agents/cloud",
            {"name": "Mary", "provider": "exe:hermes", "chat_uids": ["cht_a"]},
        )
    ]


def test_cloud_list_emits_resources(run, cloud_server) -> None:
    resources = _contract_resources()[:2]
    cloud_server.respond(resources)

    result = run("--json", "cloud-list", env=cloud_server.environment)

    assert result.returncode == 0
    assert _json_document(result, "cloud-list")["result"] == {"agents": resources}
    assert cloud_server.requests == [("GET", "/v1/agents/cloud", None)]


def test_cloud_get_emits_a_resource(run, cloud_server) -> None:
    resource = _contract_resources()[0]
    cloud_server.respond(resource)

    result = run("--json", "cloud-get", resource["agent_id"], env=cloud_server.environment)

    assert result.returncode == 0
    assert _json_document(result, "cloud-get")["result"] == {"agent": resource}
    assert cloud_server.requests == [("GET", f"/v1/agents/cloud/{resource['agent_id']}", None)]


def test_cloud_update_chats_reads_the_api_request_shape_from_stdin(run, cloud_server) -> None:
    resource = _contract_resources()[0]
    cloud_server.respond(resource)
    result = run(
        "--json",
        "cloud-update-chats",
        resource["agent_id"],
        env=cloud_server.environment,
        input=json.dumps({"chat_uids": ["cht_a", "cht_b"]}),
    )

    assert result.returncode == 0
    assert _json_document(result, "cloud-update-chats")["result"] == {"agent": resource}
    assert cloud_server.requests == [
        (
            "PUT",
            f"/v1/agents/cloud/{resource['agent_id']}/chats",
            {"chat_uids": ["cht_a", "cht_b"]},
        )
    ]


def test_cloud_delete_emits_the_teardown_resource(run, cloud_server) -> None:
    resource = _contract_resources()[3]
    cloud_server.respond(resource)

    result = run("--json", "cloud-delete", resource["agent_id"], env=cloud_server.environment)

    assert result.returncode == 0
    assert _json_document(result, "cloud-delete")["result"] == {"agent": resource}
    assert cloud_server.requests == [("DELETE", f"/v1/agents/cloud/{resource['agent_id']}", None)]


@pytest.mark.parametrize(
    "contents,code",
    [
        ("not JSON", "invalid_argument"),
        (json.dumps({"chat_uids": []}), "invalid_argument"),
    ],
)
def test_cloud_create_reports_input_failures(run, contents, code) -> None:
    result = run("--json", "cloud-create", input=contents)

    assert result.returncode == 1
    body = _json_document(result, "cloud-create")
    assert body["ok"] is False
    assert body["error"]["code"] == code


def test_cloud_create_refuses_terminal_stdin(monkeypatch, capsys) -> None:
    monkeypatch.syspath_prepend(str(ROOT))
    from agent_mgr import cli

    stdin = io.StringIO("")
    monkeypatch.setattr(stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys, "stdin", stdin)

    assert cli.main(["--json", "cloud-create"]) == 2

    captured = capsys.readouterr()
    body = _json_document(captured, "cloud-create")
    assert body["error"]["code"] == "invalid_argument"
    assert "interactive terminal" in body["error"]["message"]


def test_cloud_create_rejects_lone_surrogates_as_one_json_document(
    run, cloud_server
) -> None:
    payload = r'{"chat_uids":["\ud800"]}'
    cloud_server.respond(_contract_resources()[1])
    result = run(
        "--json",
        "cloud-create",
        env=cloud_server.environment,
        input=payload,
    )

    assert result.returncode == 1
    body = _json_document(result, "cloud-create")
    assert body["error"]["code"] == "invalid_argument"
    assert body["error"]["message"] == "chat_uids must contain valid Unicode"
    assert cloud_server.requests == []


def test_cloud_create_marks_an_unreadable_success_as_ambiguous(run, cloud_server) -> None:
    cloud_server.respond_bytes(b"not-json")
    result = run(
        "--json",
        "cloud-create",
        env=cloud_server.environment,
        input=json.dumps({"chat_uids": ["cht_a"]}),
    )

    assert result.returncode == 1
    body = _json_document(result, "cloud-create")
    assert body["error"]["code"] == "invalid_response"
    assert body["error"]["remediation"] == (
        "creation may have succeeded; run agent-mgr --json cloud-list before retrying"
    )
    assert cloud_server.requests[0][0] == "POST"


def test_cloud_delete_marks_an_unreadable_success_as_ambiguous(run, cloud_server) -> None:
    resource = _contract_resources()[0]
    cloud_server.respond_bytes(b"not-json")

    result = run("--json", "cloud-delete", resource["agent_id"], env=cloud_server.environment)

    assert result.returncode == 1
    body = _json_document(result, "cloud-delete")
    assert body["error"]["code"] == "invalid_response"
    assert body["error"]["remediation"] == (
        "deletion may have succeeded; run agent-mgr --json cloud-list before retrying"
    )
    assert cloud_server.requests[0][0] == "DELETE"


def test_help_lists_every_cloud_argument_shape(run) -> None:
    result = run("--help")

    assert result.returncode == 0
    for invocation in (
        "cloud-create",
        "cloud-list",
        "cloud-get <agent-id>",
        "cloud-update-chats <agent-id>",
        "cloud-delete <agent-id>",
    ):
        assert invocation in result.stdout
