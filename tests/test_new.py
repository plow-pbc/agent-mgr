import yaml


def test_new_scaffolds_an_instance_repo_and_registers_it(run, tmp_path):
    target = tmp_path / "acme-hermes-agent"
    r = run("new", "acme", str(target))
    assert r.returncode == 0, r.stderr
    assert (target / "agent.env").exists()
    assert (target / "config.yaml").exists()
    assert "acme" in run("ls").stdout


def test_the_scaffolded_config_has_baseline_integrations_group_scope_and_fallback(
    run, tmp_path
):
    """Plow Chat and Latch are baseline, so a new agent is ready to configure,
    not ready to be wired. Group sessions are shared per chat: the image
    default (group_sessions_per_user: true) keys them per sender, splitting one
    visible iMessage thread into per-person agent contexts — the 2026-08-30
    life-assistant incident. The plow-chat plugin's config.extra flag never
    reaches the gateway session store, so the template's gateway-level key is
    what actually holds the line.

    Compression gets a fallback for the same reason: an empty chain leaves an
    oversized session with nowhere to land when the primary times out, and it
    re-stalls every turn (the ~30h freeze on 2026-08-31). The relationships
    matter, not the values — a fallback equal to the primary is discarded by
    the (provider, model)-scoped skip, and one on another provider needs a
    credential a new agent has not got. Each is inert while looking configured.

    No task-level `timeout`: the image floors a config-derived compression
    timeout at 300s (#54915), so a lower value is silently raised and a value
    that DID bite would cut a slow summary off into the deterministic context
    marker. The chain entry needs its own budget because that floor does not
    reach it — without one it inherits the 30s auxiliary default."""
    target = tmp_path / "acme-hermes-agent"
    run("new", "acme", str(target))
    text = (target / "config.yaml").read_text()
    assert "plow-chat-platform" in text
    assert "latch:" in text and "DOMO_DEVICE_UID" in text
    cfg = yaml.safe_load(text)
    assert cfg["group_sessions_per_user"] is False

    compression = cfg["auxiliary"]["compression"]
    chain = compression["fallback_chain"]
    assert chain, "an empty chain is the incident"
    assert all(e["model"] != cfg["model"]["default"] for e in chain)
    assert all(e["provider"] == cfg["model"]["provider"] for e in chain)
    assert "timeout" not in compression, (
        "a task-level compression timeout is floored to 300s by the image, so "
        "one below it is inert and one above it only lengthens the stall"
    )
    assert chain[0]["timeout"] >= 300, (
        "the floor applies to the task-level key, not to a chain entry, so "
        "without an explicit budget the fallback inherits the 30s default"
    )


def test_new_does_not_create_the_home_that_deploy_owns(run, tmp_path):
    """install-plugin and activate gate on the home existing as their "run
    deploy first" check. Pre-creating it lets activate spend a one-time
    activation into a home deploy has never prepared."""
    r = run("new", "acme", str(tmp_path / "acme-hermes-agent"))
    assert not (tmp_path / "home" / ".hermes-acme").exists()
    assert str(tmp_path / "home" / ".hermes-acme") in r.stdout, "the banner must still report it"


def test_new_refuses_a_name_that_is_already_registered(run, tmp_path):
    """Scaffolding an existing name elsewhere would repoint the registry, and the
    next documented step would install the template config over a live agent's."""
    first = tmp_path / "acme-hermes-agent"
    run("new", "acme", str(first))
    (first / "config.yaml").write_text("model:\n  provider: mine\n")
    r = run("new", "acme", str(tmp_path / "somewhere-else"))
    assert r.returncode != 0
    assert "already registered" in r.stderr
    assert str(first) in run("ls").stdout
    assert "provider: mine" in (first / "config.yaml").read_text()


def test_new_refuses_a_directory_holding_only_a_config(run, tmp_path):
    """The command writes two files, so checking one lets the other be clobbered."""
    d = tmp_path / "hand-made"
    d.mkdir()
    (d / "config.yaml").write_text("model:\n  provider: mine\n")
    r = run("new", "acme", str(d))
    assert r.returncode != 0
    assert "config.yaml" in r.stderr
    assert "provider: mine" in (d / "config.yaml").read_text()


def test_new_prints_the_bring_up_sequence(run, tmp_path):
    r = run("new", "acme", str(tmp_path / "acme-hermes-agent"))
    # No install-plugin: deploy does it, and listing it as a separate step is
    # the workflow re-running the installer over the config deploy just laid down.
    for step in ("deploy", "activate", "up", "sign-in", "check-latch"):
        assert step in r.stdout
    assert "install-plugin" not in r.stdout


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
