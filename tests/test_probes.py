import os

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
