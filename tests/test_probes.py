import os


def _fake_docker(tmp_path, http_code="200", running=True):
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    ps = "echo deadbeef" if running else ":"
    (b / "docker").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'  *"ps --status running --quiet"*) {ps} ;;\n'
        f'  *exec*) echo {http_code} ;;\n'
        'esac\nexit 0\n'
    )
    (b / "docker").chmod(0o755)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _with_latch(tmp_path, name, uid="dev_123", tok="tok_abc"):
    (tmp_path / "home" / f".hermes-{name}" / ".env").write_text(
        f'DOMO_DEVICE_UID={uid}\nDOMO_MCP_TOKEN="{tok}"\n')


def test_check_latch_skips_when_no_device_is_configured(run, instance, tmp_path):
    """Presence of the credential is the declaration: an agent that drives no Mac
    is not a failure."""
    run("register", "str", str(instance("str")))
    run("restore", "str")
    (tmp_path / "home" / ".hermes-str" / ".env").write_text("DOMO_DEVICE_UID=\nDOMO_MCP_TOKEN=\n")
    r = run("check-latch", "str", env=_fake_docker(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "no latch configured" in r.stdout


def test_check_latch_reports_reachable_when_the_relay_answers(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_fake_docker(tmp_path, http_code="200"))
    assert r.returncode == 0, r.stderr
    assert "reachable" in r.stdout


def test_a_revoked_credential_is_named_as_revoked_not_as_unreachable(run, instance, tmp_path):
    """A dead credential and a dead network need different fixes."""
    run("register", "property", str(instance("property")))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_fake_docker(tmp_path, http_code="401"))
    assert r.returncode != 0
    assert "REVOKED" in r.stderr


def test_no_answer_is_distinguished_from_a_bad_credential(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_fake_docker(tmp_path, http_code="000"))
    assert r.returncode != 0
    assert "NOT tested" in r.stderr


def test_the_token_is_never_printed_in_full(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    run("restore", "property")
    _with_latch(tmp_path, "property", tok="supersecrettokenvalue")
    r = run("check-latch", "property", env=_fake_docker(tmp_path, http_code="401"))
    assert "supersecrettokenvalue" not in (r.stdout + r.stderr)
    assert "lue" in r.stderr, "the last 3 characters identify it without disclosing it"


def test_a_half_configured_latch_names_the_missing_key(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    run("restore", "property")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_123\nDOMO_MCP_TOKEN=\n")
    r = run("check-latch", "property", env=_fake_docker(tmp_path))
    assert r.returncode != 0
    assert "DOMO_MCP_TOKEN is empty" in r.stderr


def test_check_latch_will_not_answer_from_the_host_when_the_gateway_is_down(run, instance, tmp_path):
    """A host answer is exactly the evidence entering the namespace exists to
    stop accepting."""
    run("register", "property", str(instance("property")))
    run("restore", "property")
    _with_latch(tmp_path, "property")
    r = run("check-latch", "property", env=_fake_docker(tmp_path, running=False))
    assert r.returncode != 0
    assert "not running" in r.stderr
