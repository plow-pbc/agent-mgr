import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_restore_installs_the_config_into_the_agents_home(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("restore", "rowan")
    assert r.returncode == 0, r.stderr
    installed = tmp_path / "home" / ".hermes-rowan" / "config.yaml"
    assert installed.exists()
    assert "openai-codex" in installed.read_text()


def test_restore_writes_a_dotenv_skeleton_carrying_both_platforms(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    env = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "PLOW_CHAT_TOKEN" in env
    assert "DOMO_MCP_TOKEN" in env, "latch is baseline, not an opt-in"


def test_restore_never_clobbers_an_existing_dotenv(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    env.write_text("PLOW_CHAT_TOKEN=real\n")
    run("restore", "rowan")
    assert env.read_text() == "PLOW_CHAT_TOKEN=real\n"


def test_installed_state_is_not_reachable_by_other_users(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    for f in ("config.yaml", ".env"):
        mode = (tmp_path / "home" / ".hermes-rowan" / f).stat().st_mode
        assert not (mode & stat.S_IRWXO), f"{f} is reachable by other users"


def test_restore_on_an_instance_with_no_config_is_refused(run, instance):
    run("register", "bare", str(instance("bare", config=None)))
    r = run("restore", "bare")
    assert r.returncode != 0
    assert "config.yaml" in r.stderr


def test_the_plugin_pin_is_a_sha_not_a_branch():
    """A branch would silently re-point a running agent on the next upstream push."""
    ref = (ROOT / "runtime" / "plow-chat-plugin.ref").read_text().strip()
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)


def test_the_image_pin_is_a_digest_not_a_tag():
    ref = (ROOT / "runtime" / "image.ref").read_text().strip()
    assert ref.startswith("sha256:") and len(ref) == 71


def test_install_plugin_refuses_a_ref_that_is_not_a_sha(run, instance):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    r = run("install-plugin", "rowan", env={"AGENT_MGR_PLUGIN_REF": "main"})
    assert r.returncode != 0
    assert "40-char SHA" in r.stderr


def test_the_shipped_config_template_wires_both_platforms():
    cfg = (ROOT / "templates" / "config.yaml").read_text()
    assert "plow-chat-platform" in cfg
    assert "latch:" in cfg and "DOMO_DEVICE_UID" in cfg


def test_no_template_carries_a_literal_credential():
    for name in ("config.yaml", "env.example", "agent.env"):
        text = (ROOT / "templates" / name).read_text()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for key in ("PLOW_CHAT_TOKEN", "DOMO_MCP_TOKEN"):
                if line.strip().startswith(f"{key}="):
                    assert line.strip() == f"{key}=", f"{name} ships a value for {key}"
