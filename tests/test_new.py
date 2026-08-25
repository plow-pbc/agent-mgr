def test_new_scaffolds_an_instance_repo_and_registers_it(run, tmp_path):
    target = tmp_path / "acme-hermes-agent"
    r = run("new", "acme", str(target))
    assert r.returncode == 0, r.stderr
    assert (target / "agent.env").exists()
    assert (target / "config.yaml").exists()
    assert "acme" in run("ls").stdout


def test_the_scaffolded_config_wires_both_plow_chat_and_latch(run, tmp_path):
    """Both are baseline, so a new agent is ready to configure, not ready to be
    wired."""
    run("new", "acme", str(tmp_path / "acme-hermes-agent"))
    cfg = (tmp_path / "acme-hermes-agent" / "config.yaml").read_text()
    assert "plow-chat-platform" in cfg
    assert "latch:" in cfg and "DOMO_DEVICE_UID" in cfg


def test_new_creates_the_agents_home(run, tmp_path):
    run("new", "acme", str(tmp_path / "acme-hermes-agent"))
    assert (tmp_path / "home" / ".hermes-acme").is_dir()


def test_new_prints_the_bring_up_sequence(run, tmp_path):
    r = run("new", "acme", str(tmp_path / "acme-hermes-agent"))
    for step in ("restore", "install-plugin", "activate", "up", "sign-in", "check-latch"):
        assert step in r.stdout


def test_new_refuses_to_overwrite_an_existing_instance(run, tmp_path):
    target = tmp_path / "acme-hermes-agent"
    run("new", "acme", str(target))
    (target / "config.yaml").write_text("model:\n  provider: mine\n")
    r = run("new", "acme", str(target))
    assert r.returncode != 0
    assert "already" in r.stderr
    assert "provider: mine" in (target / "config.yaml").read_text()


def test_a_scaffolded_agent_resolves_without_further_editing(run, tmp_path):
    """The descriptor is all-comments; every value comes from the convention."""
    run("new", "acme", str(tmp_path / "acme-hermes-agent"))
    r = run("resolve", "acme")
    assert r.returncode == 0, r.stderr
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes-acme" in r.stdout


def test_an_invalid_name_is_refused_before_anything_is_created(run, tmp_path):
    r = run("new", "Acme Corp", str(tmp_path / "x"))
    assert r.returncode != 0
    assert not (tmp_path / "x").exists()
