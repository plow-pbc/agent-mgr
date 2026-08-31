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
        ("cloud-set-line", ("agent-id",)),
    ],
)
def test_the_body_carrying_cloud_commands_require_json(
    operation: str, args: tuple[str, ...], run
) -> None:
    """Only the two verbs that read a request body still demand --json.

    The rest answer a name, so requiring it of the whole namespace made one
    target machine-only and the other human-usable -- the split this CLI exists
    to close.
    """
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
        input=json.dumps({"name": "Mary", "provider": "exe:hermes", "line_uid": "ln_p3"}),
    )

    assert result.returncode == 0
    body = _json_document(result, "cloud-create")
    assert body["result"]["agent"] == resource
    assert cloud_server.requests == [
        (
            "POST",
            "/v1/agents/cloud",
            {"name": "Mary", "provider": "exe:hermes", "line_uid": "ln_p3"},
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


def test_cloud_update_line_reads_the_api_request_shape_from_stdin(run, cloud_server) -> None:
    resource = _contract_resources()[0]
    cloud_server.respond(resource)
    result = run(
        "--json",
        "cloud-set-line",
        resource["agent_id"],
        env=cloud_server.environment,
        input=json.dumps({"line_uid": "ln_p4"}),
    )

    assert result.returncode == 0
    assert _json_document(result, "cloud-set-line")["result"] == {"agent": resource}
    assert cloud_server.requests == [
        (
            "PUT",
            f"/v1/agents/cloud/{resource['agent_id']}/line",
            {"line_uid": "ln_p4"},
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
        (json.dumps({"line_uid": ""}), "invalid_argument"),
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


def test_cloud_create_rejects_lone_surrogates_as_one_json_document(run, cloud_server) -> None:
    payload = r'{"line_uid":"\ud800"}'
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
    assert body["error"]["message"] == "line_uid must contain valid Unicode"
    assert cloud_server.requests == []


def test_cloud_create_marks_an_unreadable_success_as_ambiguous(run, cloud_server) -> None:
    cloud_server.respond_bytes(b"not-json")
    result = run(
        "--json",
        "cloud-create",
        env=cloud_server.environment,
        input=json.dumps({"line_uid": "ln_p3"}),
    )

    assert result.returncode == 1
    body = _json_document(result, "cloud-create")
    assert body["error"]["code"] == "invalid_response"
    assert body["error"]["remediation"] == (
        "creation may have succeeded; run agent-mgr cloud-list before retrying"
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
        "deletion may have succeeded; run agent-mgr cloud-list before retrying"
    )
    assert cloud_server.requests[0][0] == "DELETE"


def test_help_lists_every_cloud_argument_shape(run) -> None:
    result = run("--help")

    assert result.returncode == 0
    for invocation in (
        "cloud-create",
        "cloud-list",
        "cloud-get",
        "cloud-set-line",
        "cloud-delete",
        "register-cloud",
    ):
        assert invocation in result.stdout
    # The help has to say what a cloud target cannot do, or `restart` reads as
    # available on both and its refusal looks like a bug.
    assert "restart and logs have no exe equivalent" in result.stdout


def test_a_registered_cloud_agent_answers_the_local_lifecycle_verbs(run, cloud_server) -> None:
    """`up` and `chats` name a registered agent, whichever target it is.

    Before this, the cloud half was a separate `cloud-*` namespace addressed by
    raw agent id, so a caller had to know which kind of agent it held before it
    could pick a verb -- the split this parity work removes.
    """
    resource = _contract_resources()[0]
    registered = run("register-cloud", "mary", resource["agent_id"])
    assert registered.returncode == 0
    assert f"registered mary -> cloud {resource['agent_id']}" in registered.stdout

    cloud_server.respond(resource)
    up = run("up", "mary", env=cloud_server.environment)
    assert up.returncode == 0
    assert resource["agent_id"] in up.stdout
    assert cloud_server.requests[0][0] == "GET"

    cloud_server.respond(resource)
    listed = run("chats", "mary", env=cloud_server.environment)
    assert listed.returncode == 0
    for chat in resource.get("chat_uids") or []:
        assert chat in listed.stdout


def test_ls_names_each_agents_target(run, cloud_server, tmp_path) -> None:
    """A row that does not say which target it is makes every verb a guess."""
    repo = tmp_path / "local-agent"
    repo.mkdir()
    assert run("register", "localone", str(repo)).returncode == 0
    assert run("register-cloud", "cloudone", "abc123").returncode == 0

    result = run("ls")
    assert result.returncode == 0
    assert "TARGET" in result.stdout
    rows = {line.split()[0]: line for line in result.stdout.splitlines() if line.split()}
    assert "local" in rows["localone"]
    assert "cloud" in rows["cloudone"]
    assert "abc123" in rows["cloudone"]


def test_restart_and_logs_refuse_a_cloud_agent_by_name(run) -> None:
    """Refusing beats silently meaning something else per target: an exe restart
    would delete and re-create the tenant, minting a credential and stranding
    its chat, and there is no exe log surface at all."""
    assert run("register-cloud", "mary", "abc123").returncode == 0
    for operation, reason in (("restart", "delete and re-create"), ("logs", "no log surface")):
        result = run(operation, "mary")
        assert result.returncode != 0
        assert reason in result.stderr


def test_a_two_field_registry_row_still_reads_as_local(run, registry, tmp_path) -> None:
    """Rows written before targets existed carry no third field. They are read,
    never migrated, so an older agent-mgr sharing this file keeps working."""
    repo = tmp_path / "legacy"
    repo.mkdir()
    Path(registry).parent.mkdir(parents=True, exist_ok=True)
    Path(registry).write_text(f"legacy\t{repo}\n", encoding="utf-8")

    result = run("ls")
    assert result.returncode == 0
    assert "legacy" in result.stdout
    assert "local" in result.stdout


def test_json_logs_on_a_cloud_agent_says_why_not_retry_without_json(run) -> None:
    """The generic unbounded-output gate says "run it without --json", which is
    sound for a local agent and a lie for a cloud one: exe publishes no log
    surface, so that retry is guaranteed to refuse for a different reason."""
    assert run("register-cloud", "mary", "abc123").returncode == 0

    result = run("--json", "logs", "mary")

    assert result.returncode != 0
    assert "no log surface" in result.stdout + result.stderr
    assert "run it without --json" not in result.stdout + result.stderr


def test_json_compose_on_a_cloud_agent_does_not_prescribe_a_failing_retry(run) -> None:
    """`compose` reaches `Registry.lookup()` and refuses because a cloud agent
    has no checkout, so "run it without --json" is the same wrong next step
    `logs` used to give: the retry fails, just later and for another reason."""
    assert run("register-cloud", "mary", "abc123").returncode == 0

    result = run("--json", "compose", "mary", "ps")

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "has none" in combined
    assert "run it without --json" not in combined
