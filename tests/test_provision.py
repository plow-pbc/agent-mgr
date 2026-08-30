"""`provision` -- the credential half of one-command setup.

`activate` cannot avoid a human: `POST /v1/auth/activate` carries no credential,
so the account binding is decided by which phone texts the code back. These
cover the path that does not need one: the caller is already authenticated as
the owner, so Plow mints against that account the way cloud provisioning does.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

TOKEN = "plow_minted_tok"


class _Plow:
    """The two calls provision makes, and a record of exactly what it sent."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object]] = []
        self.chats: list[dict[str, object]] = []
        self.mint_status = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _read(self) -> object:
                length = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(length)) if length else None

            def _send(self, status: int, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                owner.requests.append(("POST", self.path, self._read()))
                if owner.mint_status != 200:
                    self._send(owner.mint_status, {"detail": "Line not found"})
                    return
                self._send(200, {"token": TOKEN, "name": "agent-mgr:rowan"})

            def do_GET(self) -> None:
                owner.requests.append(("GET", self.path, self.headers.get("Authorization")))
                self._send(200, {"data": owner.chats, "has_more": False})

            def log_message(self, *_: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def environment(self) -> dict[str, str]:
        host, port = self._server.server_address[:2]
        return {"PLOW_API_BASE": f"http://{host}:{port}", "PLOW_API_TOKEN": "account-token"}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _chat(uid: str, members: int) -> dict[str, object]:
    return {
        "uid": uid,
        "participants": [{"type": "agent"}]
        + [{"type": "member", "uid": f"mem_{n}"} for n in range(members)],
    }


@pytest.fixture
def plow() -> Iterator[_Plow]:
    server = _Plow()
    try:
        yield server
    finally:
        server.close()


def _restored(run, instance, name: str = "rowan") -> None:
    run("register", name, str(instance(name)))
    run("restore", name)


def test_provision_mints_against_the_line_and_writes_the_dotenv(
    run, instance, tmp_path, plow: _Plow
) -> None:
    """One call, no code, no phone -- the whole point of the command."""
    _restored(run, instance)
    plow.chats = [_chat("cht_group", 3), _chat("cht_home", 1)]

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode == 0, result.stderr
    method, path, body = plow.requests[0]
    assert (method, path) == ("POST", "/v1/relay/agents")
    # The grant names the LINE. A chat list cannot cover the threads that number
    # receives tomorrow, which is the failure this whole path exists to avoid.
    assert body == {"name": "agent-mgr:rowan", "line_uid": "ln_p3"}

    dotenv = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    lines = dotenv.splitlines()
    assert f"PLOW_AGENT_TOKEN={TOKEN}" in lines
    assert f"PLOW_CHAT_TOKEN={TOKEN}" in lines
    # The 1:1 is home, not the group: cron and unprompted output land there, and
    # a group home would put the owner's private deliveries in front of members.
    assert "PLOW_HOME_CHANNEL=cht_home" in lines
    assert "PLOW_CHAT_CHAT_UID=cht_home" in lines


def test_the_home_is_read_with_the_new_grant_not_the_account_token(
    run, instance, plow: _Plow
) -> None:
    """Asking as the AGENT is the same question the gateway asks on its first
    connection; asking as the account would answer for chats the grant cannot
    reach, and the agent would arrive silent on a home it cannot read."""
    _restored(run, instance)
    plow.chats = [_chat("cht_home", 1)]

    assert run("provision", "rowan", "ln_p3", env=plow.environment).returncode == 0

    listing = [request for request in plow.requests if request[0] == "GET"]
    assert listing, "provision never read the chat listing"
    assert listing[0][2] == f"Bearer {TOKEN}"


def test_provision_refuses_an_agent_that_already_holds_a_credential(
    run, instance, tmp_path, plow: _Plow
) -> None:
    """A second mint strands the credential the gateway is holding, and the live
    agent goes deaf on a line it still believes it serves."""
    _restored(run, instance)
    dotenv = tmp_path / "home" / ".hermes-rowan" / ".env"
    dotenv.write_text("PLOW_AGENT_TOKEN=already_live\n")

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert "already holds a Plow credential" in result.stderr
    assert plow.requests == [], "the refusal still called the mint"
    assert dotenv.read_text() == "PLOW_AGENT_TOKEN=already_live\n"


def test_a_refused_line_writes_nothing(run, instance, tmp_path, plow: _Plow) -> None:
    """Plow answers 404 for a line the account holds no chat on. Nothing may
    land from a mint that did not happen."""
    _restored(run, instance)
    plow.mint_status = 404
    dotenv = tmp_path / "home" / ".hermes-rowan" / ".env"
    before = dotenv.read_text()

    result = run("provision", "rowan", "ln_theirs", env=plow.environment)

    assert result.returncode != 0
    assert dotenv.read_text() == before


def test_a_grant_reaching_no_chat_is_an_error_not_a_silent_agent(
    run, instance, tmp_path, plow: _Plow
) -> None:
    """An empty listing means the credential would arrive at an agent with no
    home -- it would start, look healthy, and answer nothing."""
    _restored(run, instance)
    plow.chats = []
    dotenv = tmp_path / "home" / ".hermes-rowan" / ".env"
    before = dotenv.read_text()

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert "reaches no chat" in result.stderr
    assert dotenv.read_text() == before


def test_provision_refuses_before_restore_has_run(run, instance, plow: _Plow) -> None:
    """There is no dotenv to write into yet, and creating one here would leave a
    home no restore afterwards owns."""
    run("register", "rowan", str(instance("rowan")))

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert "restore" in result.stderr


def test_provision_needs_the_account_credential_named(run, instance) -> None:
    """The mint is authenticated as the owner; without that this is just an
    unauthenticated POST, and the failure should say which variable is missing."""
    _restored(run, instance)

    result = run("provision", "rowan", "ln_p3", env={"PLOW_API_BASE": "", "PLOW_API_TOKEN": ""})

    assert result.returncode != 0
    assert "PLOW_API_BASE" in result.stderr


def test_help_lists_provision(run) -> None:
    result = run("--help")

    assert result.returncode == 0
    assert "provision" in result.stdout
    assert os.linesep is not None


def test_two_one_to_one_chats_on_a_line_refuse_rather_than_guess(
    run, instance, tmp_path, plow: _Plow
) -> None:
    """A line carries one 1:1 per person who has texted that number. Breaking
    the tie by API order could pick ANOTHER CONTACT'S DM as home and deliver
    this owner's cron output and private replies into it -- and nothing in the
    listing says which one is theirs, so guessing is the bug."""
    _restored(run, instance)
    plow.chats = [_chat("cht_someone", 1), _chat("cht_owner", 1)]

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert "ambiguous" in result.stderr
    dotenv = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "PLOW_AGENT_TOKEN=" not in dotenv or "PLOW_AGENT_TOKEN=\n" in dotenv


def test_a_failed_reload_does_not_read_as_a_failed_mint(
    run, instance, tmp_path, plow: _Plow, monkeypatch
) -> None:
    """The mint is one-time and already on disk. Reporting the reload failure as
    a failed provision sends the operator to unregister a home whose only
    problem is a container that did not restart -- and the retry would refuse
    anyway, because the credential is there."""
    _restored(run, instance)
    plow.chats = [_chat("cht_home", 1)]
    broken = tmp_path / "brokenbin"
    broken.mkdir()
    # `docker` that always fails, so the post-write reload is what breaks.
    (broken / "docker").write_text("#!/bin/sh\nexit 1\n")
    (broken / "docker").chmod(0o755)

    result = run(
        "provision",
        "rowan",
        "ln_p3",
        env=plow.environment | {"PATH": f"{broken}:{os.environ['PATH']}"},
    )

    dotenv = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    if result.returncode != 0:
        assert "credential IS written" in result.stderr
        assert "do not re-run provision" in result.stderr
    assert f"PLOW_AGENT_TOKEN={TOKEN}" in dotenv.splitlines()
