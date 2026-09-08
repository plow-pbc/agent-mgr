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
    run("deploy", "str")
    r = run("check-latch", "str", env=_bin(tmp_path, "str"))
    assert r.returncode == 0, r.stderr
    assert "no latch configured" in r.stdout


def test_a_leftover_credential_does_not_make_an_agent_look_latch_enabled(run, instance, tmp_path):
    """Found in production: the rentals agent's dotenv still carried a DOMO_*
    pair from an earlier experiment while its config declared no latch server.
    Keying off the credential probed a relay it cannot reach and reported a
    revoked token as a failure of an agent that never used one."""
    run("register", "str", str(instance("str", config=NO_LATCH_CONFIG)))
    run("deploy", "str")
    (tmp_path / "home" / ".hermes-str" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_stale\nDOMO_MCP_TOKEN=tok_revoked\n")
    r = run("check-latch", "str", env=_bin(tmp_path, "str", exec_output="401"))
    assert r.returncode == 0, r.stderr
    assert "no latch configured" in r.stdout
    assert "REVOKED" not in r.stderr


def test_check_latch_reports_reachable_when_the_relay_answers(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="200"))
    assert r.returncode == 0, r.stderr
    assert "reachable" in r.stdout


@pytest.mark.parametrize(
    "dotenv,relay_env,expected_uid,rejected_uid",
    [
        # An instance whose compose.override.yml supplies DOMO_* through
        # env_file has a working relay and a home dotenv that declares neither
        # key. Reading only the dotenv called that agent unconfigured and told
        # the operator to re-mint a live credential -- which would have
        # written a second, divergent pair into a file the container does not
        # read. agent_mgr/local.py:104 promises an override's env_file
        # survives, so this is a supported shape, not a workaround.
        ("API_SERVER_KEY=keepme\n",
         {"DOMO_DEVICE_UID": "dev_from_compose", "DOMO_MCP_TOKEN": "tok_from_compose"},
         "dev_from_compose", None),
        # hermes loads $HERMES_HOME/.env with override=True
        # (hermes_cli/env_loader.py:500), so a key in the dotenv is the one
        # the gateway ends up using. Probing the container's value instead
        # would report REVOKED for a live credential whenever the two
        # disagree -- which is exactly what a half-finished set-latch leaves
        # behind.
        ("DOMO_DEVICE_UID=dev_dotenv\nDOMO_MCP_TOKEN=tok_dotenv\n",
         {"DOMO_DEVICE_UID": "dev_stale", "DOMO_MCP_TOKEN": "tok_stale"},
         "dev_dotenv", "dev_stale"),
    ],
    ids=["container-supplies-an-unconfigured-dotenv", "dotenv-outranks-the-container"],
)
def test_the_pair_resolves_dotenv_first_container_fallback(
        run, instance, tmp_path, dotenv, relay_env, expected_uid, rejected_uid):
    """Two rows of one arrange/act, differing only in who is supposed to win.

    Both assert on the bytes that reached curl, not just the exit code: the
    fake relay answers 200 to anything, so an exit-code-only assertion pins
    that a uid was found, never that it was the RIGHT one."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(dotenv)
    log = tmp_path / "docker.log"
    r = run("check-latch", "property", env=_bin(
        tmp_path, "property", exec_output="200", log=log, relay_env=relay_env))
    assert r.returncode == 0, r.stderr
    assert "reachable" in r.stdout
    curl_argv = (tmp_path / "docker.log.curlargv").read_text()
    assert expected_uid in curl_argv
    if rejected_uid:
        assert rejected_uid not in curl_argv, "the probe used the wrong source's value"


def test_a_revoked_credential_is_named_as_revoked_not_as_unreachable(run, instance, tmp_path):
    """A dead credential and a dead network need different fixes."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="401"))
    assert r.returncode != 0
    assert "REVOKED" in r.stderr


def test_no_answer_is_distinguished_from_a_bad_credential(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="000"))
    assert r.returncode != 0
    assert "NOT tested" in r.stderr


def test_the_token_is_never_printed_in_full(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    _with_latch(tmp_path, "property", tok="supersecrettokenvalue")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="401"))
    assert "supersecrettokenvalue" not in (r.stdout + r.stderr)
    assert "lue" not in r.stderr, "credential-derived suffixes must not reach shared logs"


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
    the probe, readable by `ps` from any account on the host. It goes in on
    stdin instead -- and, inside the container, into curl's --config over a
    pipe rather than a file at rest, so it is absent from `ps` and from disk
    on both sides.

    And it must be the value the GATEWAY loaded. Asserted on the bytes that
    reached curl rather than the exit code, because the fake relay answers 200
    to anything -- an exit-code assertion pins that a line was found, never that
    the right one was."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
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
    assert f"DOTENV_TOK={expected}" in stdin or f"DOTENV_TOK='{expected}'" in stdin
    assert "stale_first" not in stdin
    # Inside the container too: the bearer is piped into curl's --config, so it
    # must not appear in curl's own argv...
    assert expected not in (tmp_path / "docker.log.curlargv").read_text()
    # ...but it MUST be the config curl actually read. Absent from argv proves
    # nothing about whether it ever reached curl at all -- misspell `header` or
    # drop `--config` and this is the only assertion that would catch it, by
    # failing where check-latch would otherwise report a live credential REVOKED.
    curlconfig = (tmp_path / "docker.log.curlconfig").read_text()
    assert f'header = "Authorization: Bearer {expected}"' in curlconfig


def test_a_half_configured_latch_names_the_missing_key(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=\n")
    r = run("check-latch", "property", env=_bin(tmp_path, "property"))
    assert r.returncode != 0
    assert "DOMO_MCP_TOKEN is empty" in r.stderr


def test_a_blank_dotenv_declaration_clobbers_a_working_container_credential(
        run, instance, tmp_path):
    """hermes' own load_dotenv(override=True) keys off PRESENCE: a
    DOMO_MCP_TOKEN= line with no value still clobbers whatever the container's
    env_file supplied, to "". Falling through to the container on emptiness
    alone read that blank declaration as absent and probed the container's
    still-good token instead -- reporting a latch the gateway cannot actually
    use as reachable, a false green on a health probe (#166)."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=\n")
    r = run("check-latch", "property", env=_bin(
        tmp_path, "property", exec_output="200",
        relay_env={"DOMO_DEVICE_UID": "dev_from_compose", "DOMO_MCP_TOKEN": "tok_from_compose"}))
    assert r.returncode != 0
    assert "DOMO_MCP_TOKEN is empty" in r.stderr


def test_check_latch_will_not_answer_from_the_host_when_the_gateway_is_down(run, instance, tmp_path):
    """A host answer is exactly the evidence entering the namespace exists to
    stop accepting."""
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
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
            # Matched on the presence test, not the skill's directory: this
            # fixture mirrors a behaviour -- "missing skill is named once" --
            # and pinning the path here only re-breaks it on the next move.
            '  *"test -f "*plow_connector.py*)\n'
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
    import sys

    root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from agent_mgr.descriptor import OPTIONAL_PATH_KEYS
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
    for hook in sorted(OPTIONAL_PATH_KEYS):
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
    run("deploy", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=   \n")
    r = run("check-latch", "property", env=_bin(tmp_path, "property", exec_output="200"))
    assert r.returncode != 0
    assert "DOMO_MCP_TOKEN is empty" in r.stderr


def test_a_swapped_dotenv_cannot_send_a_sibling_token_into_the_container(run, instance, tmp_path):
    run("register", "property", str(instance("property", config=LATCH_CONFIG)))
    run("deploy", "property")
    secret = tmp_path / "sibling.env"
    secret.write_text("DOMO_DEVICE_UID=dev_sibling\nDOMO_MCP_TOKEN=tok_sibling\n")
    dotenv = tmp_path / "home" / ".hermes-property" / ".env"
    dotenv.unlink()
    dotenv.symlink_to(secret)
    log = tmp_path / "docker.log"
    r = run("check-latch", "property",
            env=_bin(tmp_path, "property", log=log, exec_output="200"))
    assert r.returncode != 0
    assert not log.exists() or "tok_sibling" not in log.read_text()


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
    run("deploy", "property")
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


# --- chats / set-home ---------------------------------------------------------

def _chats_response(*uids_and_names):
    """The /v1/chats body the fake curl serves, with the HTTP code on the line
    the real probe appends via `-w '\n%{http_code}'`. Quoted, because the fake
    docker interpolates exec_output into an unquoted `echo`."""
    import json
    from conftest import shlex_quote
    data = [{
        "uid": uid,
        "display_name": name,
        "participants": [
            {"type": "agent", "line": {"uid": "ln_p2", "provider_key": "+16505550100"}},
            {"type": "member", "display_name": "Sam"},
        ],
    } for uid, name in uids_and_names]
    return shlex_quote(json.dumps({"data": data}) + "\n200")


def _with_plow(run, instance, tmp_path, home_uid="cht_old_dm"):
    """Register + deploy `property`, and mint it the credential `activate`
    writes -- outside the home, which is the only place the token lives now."""
    run("register", "property", str(instance("property")))
    run("deploy", "property")
    env_file = tmp_path / "home" / ".hermes-property" / ".env"
    env_file.write_text(f"HOSTEX_TOKEN=keepme\nPLOW_HOME_CHANNEL={home_uid}\n")
    (tmp_path / "home" / ".plow-credentials-property").write_text(
        "PLOW_API_BASE=https://api.plow.co\nPLOW_AGENT_TOKEN=tok_plow\n")
    return env_file


def test_chats_reads_the_token_from_the_credential_file(run, instance, tmp_path):
    """One source, under either boot contract -- so `chats` derives no
    contract at all, and the mid-migration row this used to carry asks the
    same question as this one.

    The dotenv was the legacy contract's copy, and the current gateway
    truncates these keys out of it on every boot: what is left there is a
    revoked shadow, and reading it is what took the STR agent offline (#174).
    PLOW_HOME_CHANNEL is unaffected -- it is not a truncated key."""
    _with_plow(run, instance, tmp_path)
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "HOSTEX_TOKEN=keepme\nPLOW_HOME_CHANNEL=cht_old_dm\nPLOW_AGENT_TOKEN=tok_shadow\n")
    log = tmp_path / "docker.log"
    r = run("chats", "property", env=_bin(
        tmp_path, "property", log=log, home_env="/var/lib/hermes",
        exec_output=_chats_response(("cht_old_dm", None))))
    assert r.returncode == 0, r.stderr
    assert "cht_old_dm" in r.stdout
    piped = Path(f"{log}.stdin").read_text()
    assert "Bearer tok_plow" in piped
    assert "tok_shadow" not in piped


def test_chats_marks_the_home_and_keeps_the_token_off_argv(run, instance, tmp_path):
    _with_plow(run, instance, tmp_path)
    log = tmp_path / "docker.log"
    r = run("chats", "property", env=_bin(
        tmp_path, "property", log=log,
        exec_output=_chats_response(("cht_old_dm", None), ("cht_group", "STR Owners"))))
    assert r.returncode == 0, r.stderr
    marked = [l for l in r.stdout.splitlines() if l.startswith("*")]
    assert len(marked) == 1 and "cht_old_dm" in marked[0]
    assert "STR Owners" in r.stdout and "ln_p2" in r.stdout
    # Same contract as check-latch: the credential rides stdin, never argv.
    assert "tok_plow" not in log.read_text()
    assert 'header = "Authorization: Bearer tok_plow"' in (tmp_path / "docker.log.stdin").read_text()


def test_set_home_refuses_a_malformed_uid_before_touching_docker(run, instance, tmp_path):
    _with_plow(run, instance, tmp_path)
    r = run("set-home", "property", "banana", env=_bin(tmp_path, "property"))
    assert r.returncode != 0
    assert "cht_" in r.stderr


def test_set_home_refuses_a_uid_the_token_cannot_see(run, instance, tmp_path):
    """A foreign or mistyped uid written to the dotenv takes the agent off its
    chat entirely; the membership gate is what stands between a typo and that."""
    _with_plow(run, instance, tmp_path)
    env_file = tmp_path / "home" / ".hermes-property" / ".env"
    before = env_file.read_text()
    r = run("set-home", "property", "cht_not_mine", env=_bin(
        tmp_path, "property", exec_output=_chats_response(("cht_old_dm", None))))
    assert r.returncode != 0
    assert "not among this token's chats" in r.stderr
    assert env_file.read_text() == before


def test_set_home_writes_the_home_and_carries_every_other_key_through(run, instance, tmp_path):
    """The single-key upsert-env invocation, through the production path -- every
    other success-path caller drives the two-key DOMO pair."""
    _with_plow(run, instance, tmp_path, home_uid="cht_new_dm")
    r = run("set-home", "property", "cht_old_dm", env=_bin(
        tmp_path, "property",
        exec_output=_chats_response(("cht_old_dm", None), ("cht_new_dm", None))))
    assert r.returncode == 0, r.stderr
    lines = (tmp_path / "home" / ".hermes-property" / ".env").read_text().splitlines()
    assert "PLOW_HOME_CHANNEL=cht_old_dm" in lines
    # Both names during the compatibility window: a still-pinned pre-rename
    # plugin reads only the legacy key, so writing the new one alone reports
    # success and leaves the home un-moved.
    assert "PLOW_CHAT_CHAT_UID=cht_old_dm" in lines
    assert "HOSTEX_TOKEN=keepme" in lines


@pytest.mark.parametrize("command, extra", [("chats", ()), ("set-home", ("cht_old_dm",))])
def test_a_dead_token_dies_with_its_own_message_not_a_traceback(run, instance, tmp_path, command, extra):
    """The fetch's die must survive to the operator. Piped into the Python
    check it only exits a subshell -- empty stdin then raises a traceback and,
    for set-home, the not-among-chats diagnosis blames a uid typo for a dead
    token. That piped form reads as equivalent, which is how it shipped once."""
    from conftest import shlex_quote
    _with_plow(run, instance, tmp_path)
    r = run(command, "property", *extra, env=_bin(
        tmp_path, "property", exec_output=shlex_quote('{"detail":"bad token"}\n401')))
    assert r.returncode != 0
    assert "may be dead" in r.stderr
    assert "Traceback" not in r.stderr
    assert "not among this token's chats" not in r.stderr


def test_a_schema_break_is_not_reported_as_a_uid_typo(run, instance, tmp_path):
    """A 200 body without `data` is an upstream schema break. Routed through a
    shell `|| die` it borrowed the not-among-chats diagnosis, sending the
    operator to re-check a uid that was never the problem."""
    from conftest import shlex_quote
    _with_plow(run, instance, tmp_path)
    r = run("set-home", "property", "cht_old_dm", env=_bin(
        tmp_path, "property", exec_output=shlex_quote('{"chats":[]}\n200')))
    assert r.returncode != 0
    assert "not among this token's chats" not in r.stderr
