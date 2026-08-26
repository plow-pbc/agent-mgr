import os

import pytest
from conftest import fake_docker


def _bin(tmp_path, name, **kw):
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, **kw)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _with_latch(tmp_path, name, uid="dev_123", tok="tok_abc"):
    (tmp_path / "home" / f".hermes-{name}" / ".env").write_text(
        f'DOMO_DEVICE_UID={uid}\nDOMO_MCP_TOKEN="{tok}"\n')


LATCH_CONFIG = ("model:\n  provider: openai-codex\nmcp_servers:\n  latch:\n"
                "    url: https://api.plow.co/v1/relay/devices/${DOMO_DEVICE_UID}/mcp\n")
NO_LATCH_CONFIG = ("model:\n  provider: openai-codex\nmcp_servers:\n  hostex:\n"
                   "    url: https://example.invalid\n")


def test_check_latch_skips_when_the_config_declares_no_latch_server(run, instance, tmp_path):
    """An agent that drives no Mac is not a failure."""
    run("register", "str", str(instance("str", config=NO_LATCH_CONFIG)))
    run("restore", "str")
    r = run("check-latch", "str", env=_bin(tmp_path, "str"))
    assert r.returncode == 0, r.stderr
    assert "no latch configured" in r.stdout


def test_a_leftover_credential_does_not_make_an_agent_look_latch_enabled(run, instance, tmp_path):
    """Found in production: the rentals agent's dotenv still carried a DOMO_*
    pair from an earlier experiment while its config declared no latch server.
    Keying off the credential probed a relay it cannot reach and reported a
    revoked token as a failure of an agent that never used one."""
    run("register", "str", str(instance("str", config=NO_LATCH_CONFIG)))
    run("restore", "str")
    (tmp_path / "home" / ".hermes-str" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_stale\nDOMO_MCP_TOKEN=tok_revoked\n")
    r = run("check-latch", "str", env=_bin(tmp_path, "str", exec_output="401"))
    assert r.returncode == 0, r.stderr
    assert "no latch configured" in r.stdout
    assert "REVOKED" not in r.stderr


def test_check_latch_reports_reachable_when_the_relay_answers(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="200"))
    assert r.returncode == 0, r.stderr
    assert "reachable" in r.stdout


def test_a_revoked_credential_is_named_as_revoked_not_as_unreachable(run, instance, tmp_path):
    """A dead credential and a dead network need different fixes."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="401"))
    assert r.returncode != 0
    assert "REVOKED" in r.stderr


def test_no_answer_is_distinguished_from_a_bad_credential(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="000"))
    assert r.returncode != 0
    assert "NOT tested" in r.stderr


def test_the_token_is_never_printed_in_full(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    _with_latch(tmp_path, "property", tok="supersecrettokenvalue")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="401"))
    assert "supersecrettokenvalue" not in (r.stdout + r.stderr)
    assert "lue" in r.stderr, "the last 3 characters identify it without disclosing it"


def test_the_token_never_reaches_the_process_table(run, instance, tmp_path):
    """A different disclosure than printing it. Passed as `-H "Authorization:
    Bearer $tok"`, the live relay credential sits in the argv of `docker compose
    exec` for the length of the probe -- readable by `ps` from any account on
    the host. It goes in on stdin as a curl config instead, so the recorded argv
    must not carry it."""
    import os
    from conftest import fake_docker
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    _with_latch(tmp_path, "property", tok="supersecrettokenvalue")
    log = tmp_path / "docker.log"
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-property",
                    name="property", exec_output="200", log=log)
    run("check-latch", "property", env={"PATH": f"{b}:{os.environ['PATH']}"})
    argv = log.read_text()
    assert "exec -T" in argv, "no -T, so docker would allocate a TTY and refuse the pipe"
    assert "supersecrettokenvalue" not in argv, "the token was passed in argv"
    # And the other half: it must still REACH curl. Misspell the config keyword
    # or lose the -T and the probe gets an unauthenticated 401, which check-latch
    # reports as REVOKED -- sending the operator to mint a replacement for a
    # credential that was never sent.
    stdin = (tmp_path / "docker.log.stdin").read_text()
    assert 'header = "Authorization: Bearer supersecrettokenvalue"' in stdin


def test_a_half_configured_latch_names_the_missing_key(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=\n")
    r = run("check-latch", "property", env=_bin(tmp_path, "property"))
    assert r.returncode != 0
    assert "DOMO_MCP_TOKEN is empty" in r.stderr


def test_check_latch_will_not_answer_from_the_host_when_the_gateway_is_down(run, instance, tmp_path):
    """A host answer is exactly the evidence entering the namespace exists to
    stop accepting."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", running=False))
    assert r.returncode != 0
    assert "not running" in r.stderr


def _connectors_bin(tmp_path, name="rowan", *, script_present=True, gmail="ok", slack="ok"):
    """A docker whose `exec` answers the script-presence test and each probe."""
    import os
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name)
    (b / "docker").write_text(
        (b / "docker").read_text().replace(
            "esac",
            '  *"test -f /opt/data/skills/plow-connectors/plow_connector.py"*)\n'
            f'    exit {0 if script_present else 1} ;;\n'
            f'  *gmail*) echo \'{gmail}\'; exit {0 if gmail != "FAIL" else 1} ;;\n'
            f'  *slack*) echo \'{slack}\'; exit {0 if slack != "FAIL" else 1} ;;\n'
            "esac", 1))
    (b / "docker").chmod(0o755)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def test_check_connectors_reports_each_connector(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("check-connectors", "rowan",
            env=_connectors_bin(tmp_path, gmail="connected:true", slack="connected:false"))
    assert r.returncode == 0, r.stderr
    assert "gmail: connected:true" in r.stdout
    # connected:false is a real answer, not a failure -- it means the connector
    # is not linked to that Plow account yet.
    assert "slack: connected:false" in r.stdout


def test_a_connector_whose_probe_cannot_run_makes_the_command_fail(run, instance, tmp_path):
    """The one-connector-fails exit path: a probe that did not run is not the
    same as a connector reporting it is unlinked."""
    run("register", "rowan", str(instance("rowan")))
    r = run("check-connectors", "rowan", env=_connectors_bin(tmp_path, slack="FAIL"))
    assert r.returncode != 0
    assert "probe did not run" in r.stderr


def test_a_missing_connector_skill_is_named_rather_than_reported_per_connector(run, instance, tmp_path):
    """Without the presence check every connector reports the same generic 'no
    such file', which is the least informative way to say 'not installed'."""
    run("register", "rowan", str(instance("rowan")))
    r = run("check-connectors", "rowan", env=_connectors_bin(tmp_path, script_present=False))
    assert r.returncode != 0
    assert "plow-connectors skill is not installed" in r.stderr
    assert "add-skill" in r.stderr, "the message should name the fix"


def test_the_scaffold_and_the_docs_agree_on_what_declares_latch(run, tmp_path):
    """The scaffold ships a latch block, and check-latch reads the config rather
    than the dotenv -- so the docs must not tell a no-Mac agent to leave DOMO_*
    blank. That combination is a declared latch with no credential, which fails."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    run("new", "acme", str(tmp_path / "acme-hermes-agent"))
    cfg = (tmp_path / "acme-hermes-agent" / "config.yaml").read_text()
    assert "latch:" in cfg
    assert "deletes it" in cfg or "delete" in cfg.lower(), (
        "the scaffolded config must say how to opt out")
    for doc in ("templates/env.example", "docs/HOWTO.md"):
        text = (root / doc).read_text()
        assert "Leave both blank" not in text, f"{doc} still promises blank means unconfigured"
        assert "latch:` block" in text and "delet" in text, (
            f"{doc} does not say how to opt out")


def test_every_hook_the_resolver_declares_is_named_in_the_readmes_file_table():
    """AGENT_PRE_TRANSITION reached AGENT_KEYS one commit before the README
    table learned about it. That table is the single owner of the agent-repo
    contract, so the next hook must not be able to land without a row."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent
    # The resolver's own loop, not a name heuristic: it is what decides which
    # keys are agent-supplied executables rather than derived values.
    loop = re.search(r"^\s*for _hook in ([A-Z_ ]+); do$",
                     (root / "lib" / "common.sh").read_text(), re.M)
    assert loop, "the hook loop moved -- this probe reads it to know what to check"
    # The TABLE, not the file: a hook mentioned only in prose or an example block
    # would satisfy a whole-README grep while the row the contract lives in stays
    # missing -- which is the way a third hook would realistically land.
    section = (root / "README.md").read_text().split("## What belongs in an agent's repo")
    assert len(section) == 2, "the agent-repo section moved -- this probe reads its table"
    rows = "\n".join(l for l in section[1].splitlines() if l.startswith("|"))
    assert rows, "the agent-repo section no longer has a table"
    descriptor = (root / "templates" / "agent.env").read_text()
    for hook in loop.group(1).split():
        assert f"`{hook}`" in rows, (
            f"{hook} is a declared hook but the agent-repo table does not name it")
        # The descriptor is where an author actually meets the hook: AGENT_PRE_TRANSITION
        # reached the resolver while this template still documented one hook, so a
        # scaffolded repo could not discover the veto it is entitled to.
        assert hook in descriptor, (
            f"{hook} is a declared hook but templates/agent.env does not document it")


@pytest.mark.parametrize(
    "dotenv",
    [
        'DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN="tok_abc"\n',
        "  DOMO_DEVICE_UID=dev_123\n  DOMO_MCP_TOKEN=tok_abc\n",
        "export DOMO_DEVICE_UID=dev_123\nexport DOMO_MCP_TOKEN=tok_abc\n",
        "DOMO_DEVICE_UID = dev_123\nDOMO_MCP_TOKEN = tok_abc\n",
    ],
    ids=["bare", "indented", "export", "spaced"],
)
def test_check_latch_reads_every_spelling_the_gateway_honours(run, instance, tmp_path, dotenv):
    """check-latch is the command for diagnosing a hand-edited dotenv, so the
    spellings a hand edit arrives in are exactly the ones it must not miss. Its
    grammar is the gateway's own (hermes_cli/config.py load_env): strip the
    line, drop `export `, strip space around the key, last declaration wins."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(dotenv)
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="200"))
    assert r.returncode == 0, r.stderr
    assert "reachable" in r.stdout
    # Not "is empty" -- the credential is right there in a spelling the gateway
    # loads, and reporting it missing sends the operator to re-mint a live pair.
    assert "is empty" not in r.stderr


def test_check_latch_probes_the_declaration_the_gateway_actually_loaded(run, instance, tmp_path):
    """With two declarations of one key the gateway takes the LAST -- it assigns
    into a dict as it reads (hermes_cli/config.py load_env). Probing the first
    tests a token nothing is using: a stale credential reports REVOKED and sends
    the operator to replace a live one, or answers 200 for a token the gateway
    never loaded. Asserted on what reached curl, not on the exit code, because
    both tokens produce the same 200 from the fake relay."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=stale_first\nexport DOMO_MCP_TOKEN=live_last\n")
    log = tmp_path / "docker.log"
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-property",
                    name="property", exec_output="200", log=log)
    r = run("check-latch", "property", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    stdin = (tmp_path / "docker.log.stdin").read_text()
    assert 'header = "Authorization: Bearer live_last"' in stdin
    assert "stale_first" not in stdin
