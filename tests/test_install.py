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


def test_an_agent_can_say_where_its_config_lives(run, instance, tmp_path):
    """The rentals agent keeps config.yaml under runtime/, beside the vault seed
    and SOUL it ships with. Without this it kept a second installer that
    hardcoded both the path and the home -- two owners of the thing agent-mgr
    exists to own."""
    repo = instance("str", descriptor="AGENT_CONFIG=runtime/config.yaml\n", config=None)
    (repo / "runtime").mkdir()
    (repo / "runtime" / "config.yaml").write_text("model:\n  provider: openai-codex\n")
    run("register", "str", str(repo))
    r = run("restore", "str")
    assert r.returncode == 0, r.stderr
    assert "openai-codex" in (tmp_path / "home" / ".hermes-str" / "config.yaml").read_text()


def test_a_relative_config_path_resolves_against_the_instance_repo(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_CONFIG=runtime/config.yaml\n", config=None)
    (repo / "runtime").mkdir()
    (repo / "runtime" / "config.yaml").write_text("model:\n  provider: x\n")
    run("register", "str", str(repo))
    assert f"AGENT_CONFIG={repo}/runtime/config.yaml" in run("resolve", "str").stdout


def test_a_missing_config_names_the_path_it_looked_at(run, instance):
    """The old message named a directory, which is useless when the whole point
    is that the file is somewhere else."""
    run("register", "str", str(instance("str", descriptor="AGENT_CONFIG=runtime/config.yaml\n",
                                        config=None)))
    r = run("restore", "str")
    assert r.returncode != 0
    assert "runtime/config.yaml" in r.stderr
    assert "AGENT_CONFIG" in r.stderr


def test_an_instance_dotenv_example_wins_over_the_fleet_template(run, instance, tmp_path):
    """An agent with extra credentials knows its dotenv contract better than the
    fleet template does; a skeleton missing those keys is a first run that looks
    complete and is not."""
    repo = instance("str")
    (repo / ".env.example").write_text("HOSTEX_TOKEN=\nSEAM_API_KEY=\nPLOW_CHAT_TOKEN=\n")
    run("register", "str", str(repo))
    run("restore", "str")
    env = (tmp_path / "home" / ".hermes-str" / ".env").read_text()
    assert "HOSTEX_TOKEN" in env and "SEAM_API_KEY" in env


def test_the_fleet_template_is_used_when_an_instance_ships_none(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    env = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "PLOW_CHAT_TOKEN" in env and "DOMO_MCP_TOKEN" in env
