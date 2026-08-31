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
        self.token = TOKEN
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
                self._send(200, {"token": owner.token, "name": "agent-mgr:rowan"})

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
    # Read as the AGENT, not the account: that is the question the gateway asks
    # on its first connection, so a grant reaching nothing fails here rather
    # than arriving as an agent that starts, looks healthy and answers nothing.
    listing = [request for request in plow.requests if request[0] == "GET"]
    assert listing and listing[0][2] == f"Bearer {TOKEN}"


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


@pytest.mark.parametrize(
    ("chats", "diagnostic", "recovery"),
    [
        ([_chat("cht_someone", 1), _chat("cht_owner", 1)], "ambiguous", "up rowan"),
        ([], "reaches no chat", "give ln_p3 a chat"),
    ],
    ids=["two-one-to-ones", "empty-grant"],
)
def test_post_mint_refusal_preserves_the_token_and_names_a_usable_recovery(
    run, instance, tmp_path, plow: _Plow, chats, diagnostic, recovery
) -> None:
    """Both post-mint refusals owe the operator the same two things.

    The token must already be on disk -- the mint is one-time, so refusing
    after receiving it and before writing it destroys something nobody can get
    back -- and the recovery named must be one that can actually run. They
    differ only in WHY discovery failed and therefore what to do about it:
    ambiguity is `set-home`'s case because the chats exist, an empty grant is
    not, because nothing is reachable until the line carries one.
    """
    _restored(run, instance)
    plow.chats = chats

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert recovery in result.stderr
    assert "credential IS written" in result.stderr
    lines = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text().splitlines()
    assert f"PLOW_AGENT_TOKEN={TOKEN}" in lines
    # Present but empty is what `restore` seeds; what must not happen is a home
    # invented from a listing that never named one.
    assert "PLOW_HOME_CHANNEL=" in lines


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


def test_a_legacy_only_dotenv_still_blocks_a_second_mint(
    run, instance, tmp_path, plow: _Plow
) -> None:
    """An agent whose dotenv predates the canonical name carries only
    `PLOW_CHAT_TOKEN`. Checking `PLOW_AGENT_TOKEN` alone let a second mint
    through, stranding the credential the gateway is actually holding."""
    _restored(run, instance)
    dotenv = tmp_path / "home" / ".hermes-rowan" / ".env"
    dotenv.write_text("PLOW_CHAT_TOKEN=legacy_live\n")

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert "already holds a Plow credential" in result.stderr
    assert plow.requests == [], "the refusal still called the mint"
    assert dotenv.read_text() == "PLOW_CHAT_TOKEN=legacy_live\n"


@pytest.mark.parametrize(
    "token",
    ["plow_broken\ninjected", "plow_broken\x07bell", "plow_br\u00f6ken"],
    ids=["newline", "control", "non-ascii"],
)
def test_an_unusable_minted_token_is_refused_before_anything_is_written(
    run, instance, tmp_path, plow: _Plow, token
) -> None:
    """The transport's own rule, applied before the write. A bespoke CR/LF check
    accepted control and non-ASCII characters the transport refuses, so the
    token was persisted and only then rejected -- after the one-time mint was
    already spent. `upsert-env` also reads one value per line, so a newline
    would shift the token's tail into the next key."""
    _restored(run, instance)
    plow.token = token
    plow.chats = [_chat("cht_home", 1)]
    dotenv = tmp_path / "home" / ".hermes-rowan" / ".env"
    before = dotenv.read_text()

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert "unusable token" in result.stderr
    assert dotenv.read_text() == before


def test_a_groups_only_line_is_not_treated_as_an_ambiguous_home(
    run, instance, tmp_path, plow: _Plow
) -> None:
    """A line carrying only groups has no private home at all, which is a
    different problem from too many candidates -- and the ambiguity recovery,
    `set-home`, accepts any listed uid INCLUDING a group, which would put the
    owner's cron output and unprompted replies in front of every member."""
    _restored(run, instance)
    plow.chats = [_chat("cht_group_a", 3), _chat("cht_group_b", 4)]

    result = run("provision", "rowan", "ln_p3", env=plow.environment)

    assert result.returncode != 0
    assert "only group chats" in result.stderr
    assert "ambiguous" not in result.stderr
    assert (
        f"PLOW_AGENT_TOKEN={TOKEN}"
        in (tmp_path / "home" / ".hermes-rowan" / ".env").read_text().splitlines()
    )
