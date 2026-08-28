from pathlib import Path
import os

import pytest

from conftest import LATCH_CONFIG, fake_docker

ROOT = Path(__file__).resolve().parent.parent


def _bin(tmp_path, name, **kw):
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, **kw)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _with_latch(tmp_path, name, uid="dev_123", tok="tok_abc"):
    # Canonical `KEY=value`, the one spelling this tool writes and reads.
    (tmp_path / "home" / f".hermes-{name}" / ".env").write_text(
        f"DOMO_DEVICE_UID={uid}\nDOMO_MCP_TOKEN={tok}\n")


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


@pytest.mark.parametrize(
    "dotenv,expected",
    [
        ("DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=supersecrettokenvalue\n",
         "supersecrettokenvalue"),
        # Two canonical declarations plus a bare `=`-less line. Appending at the
        # bottom is how a duplicate happens, and the gateway takes the LAST --
        # it assigns as it reads. Probing the first would report REVOKED for a
        # live credential. The bare line used to match the key under -F= and
        # hand back its own name, which the relay 401s the same way.
        ("DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=stale_first\n"
         "DOMO_MCP_TOKEN=live_last\nDOMO_MCP_TOKEN\n", "live_last"),
    ],
    ids=["ordinary", "duplicate-and-bare"],
)
def test_check_latch_sends_the_loaded_credential_and_only_on_stdin(run, instance, tmp_path, dotenv, expected):
    """Two properties of one probe, so one setup.

    The credential must not reach argv: passed as `-H "Authorization: Bearer
    $tok"` it would sit in the argv of `docker compose exec` for the length of
    the probe, readable by `ps` from any account on the host. It goes in as a
    curl config on stdin instead.

    And it must be the value the GATEWAY loaded. Asserted on the bytes that
    reached curl rather than the exit code, because the fake relay answers 200
    to anything -- an exit-code assertion pins that a line was found, never that
    the right one was."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(dotenv)
    log = tmp_path / "docker.log"
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="200", log=log))
    assert r.returncode == 0, r.stderr
    argv = log.read_text()
    assert "exec -T" in argv, "no -T, so docker would allocate a TTY and refuse the pipe"
    assert expected not in argv, "the token was passed in argv"
    # It must still REACH curl. Misspell the config keyword or lose the -T and
    # the probe gets an unauthenticated 401, which check-latch reports as
    # REVOKED -- sending the operator to replace a credential never sent.
    stdin = (tmp_path / "docker.log.stdin").read_text()
    assert 'header = "Authorization: Bearer %s"' % expected in stdin
    assert "stale_first" not in stdin
    assert "Bearer DOMO_MCP_TOKEN" not in stdin


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
    # The resolver's own list, not a name heuristic: it is what decides which
    # keys are agent-supplied repo-relative paths rather than derived values,
    # and the path loop walks it, so a path key cannot reach the resolver
    # without passing through here.
    loop = re.search(r'^AGENT_REPO_PATHS="([A-Z_ ]+)"$',
                     (root / "lib" / "common.sh").read_text(), re.M)
    assert loop, "AGENT_REPO_PATHS moved -- this probe reads it to know what to check"
    # The TABLE, not the file: a hook mentioned only in prose or an example block
    # would satisfy a whole-README grep while the row the contract lives in stays
    # missing -- which is the way a third hook would realistically land.
    section = (root / "README.md").read_text().split("## What belongs in an agent's repo")
    assert len(section) == 2, "the agent-repo section moved -- this probe reads its table"
    rows = "\n".join(l for l in section[1].splitlines() if l.startswith("|"))
    assert rows, "the agent-repo section no longer has a table"
    descriptor = (root / "templates" / "agent.env").read_text()
    # No AGENT_KEYS membership check: AGENT_KEYS interpolates $AGENT_REPO_PATHS,
    # so a path key is carried by construction and the drift this used to police
    # cannot be written.
    for hook in loop.group(1).split():
        assert f"`{hook}`" in rows, (
            f"{hook} is a declared repo path but the agent-repo table does not name it")
        # The descriptor is where an author actually meets the hook: AGENT_PRE_TRANSITION
        # reached the resolver while this template still documented one hook, so a
        # scaffolded repo could not discover the veto it is entitled to.
        assert hook in descriptor, (
            f"{hook} is a declared hook but templates/agent.env does not document it")


def test_a_value_that_is_only_whitespace_is_reported_missing_not_probed(run, instance, tmp_path):
    """`KEY=   ` is a key with no credential. Sending it probes the relay with an
    empty bearer and reports the 401 as REVOKED, which sends the operator to
    replace a credential that was never set."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=   \n")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="200"))
    assert r.returncode != 0
    assert "DOMO_MCP_TOKEN is empty" in r.stderr


def test_an_unreadable_dotenv_is_named_as_such_not_reported_as_a_missing_credential(
        run, instance, tmp_path):
    """A dotenv check-latch cannot READ is not a dotenv with no credential in it.
    Swallowing the errno reported "DOMO_MCP_TOKEN is empty ... mint the pair",
    which sends the operator to mint a replacement and revoke a live one over a
    permission problem -- a `.env` written 600 under another account being the
    realistic way to get here.

    The diagnosis comes from parse_env_file, which load_agent runs over this
    same file for AGENT_TZ before check-latch reads a credential out of it --
    so this pins the resolver's read, not dotenv_read's. The negative assertion
    is the load-bearing one: it is what fails if the errno is ever swallowed
    back into a bare shell redirection error, which names lib/common.sh rather
    than the agent and reads as a bug in the tool rather than a permission
    problem on a file the operator can fix."""
    # Asserted rather than skipped: root reads a 000 file, so the test would
    # pass while proving nothing, and a skip hides that.
    assert os.geteuid() != 0, "run the suite unprivileged; root reads a 000 file"
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    env_file = tmp_path / "home" / ".hermes-property" / ".env"
    env_file.chmod(0o000)
    try:
        r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="200"))
    finally:
        env_file.chmod(0o600)
    assert r.returncode != 0
    assert "cannot read" in r.stderr
    assert "is empty" not in r.stderr


def test_no_doc_hardcodes_the_conventional_dotenv_path():
    """The per-person dotenv is `$AGENT_HOME/.env`, not `~/.hermes-<name>/.env`.

    Those differ for exactly the instances whose descriptor declares AGENT_HOME
    -- the case the docs most need to be right about -- and the spelling drifted
    into four operator-facing files before anyone noticed, twice. A doc that
    hardcodes the conventional path sends that operator to a file nothing reads.

    No source-string assertion guarding it: if the read ever moves,
    test_the_dotenv_follows_a_declared_home fails on behaviour, which is the
    stronger signal and the one that cannot be satisfied by a matching string.
    """
    offenders = []
    for rel in ("README.md", "docs/HOWTO.md", "templates/agent.env",
                "templates/compose.yml"):
        for n, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            if ".hermes-<name>/.env" in line or ".hermes-rowan/.env" in line:
                offenders.append(f"{rel}:{n}")
    assert not offenders, (
        "these name the conventional dotenv path, which is wrong for a "
        f"declared-home instance -- use $AGENT_HOME/.env: {offenders}"
    )
