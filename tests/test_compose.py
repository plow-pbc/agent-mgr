import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST = "nousresearch/hermes-agent@sha256:" + "c" * 64


def compose_config(tmp_path, home, name, override=None, extra_env=None):
    """Resolve the template the way agent-mgr will, and return the CompletedProcess."""
    env = dict(os.environ)
    env.update({
        "AGENT_HOME": str(home), "AGENT_CONTAINER": f"hermes-{name}",
        "AGENT_PROJECT": f"hermes-{name}", "AGENT_TZ": "America/Los_Angeles",
        "AGENT_IMAGE": DIGEST, "HERMES_UID": "1000", "HERMES_GID": "1000",
    })
    if extra_env:
        env.update(extra_env)
    files = ["-f", str(ROOT / "templates" / "compose.yml")]
    if override:
        files += ["-f", str(override)]
    return subprocess.run(["docker", "compose", *files, "config", "--format", "json"],
                          capture_output=True, text=True, env=env)


def test_the_template_resolves_one_service_bound_to_the_agents_home(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-rowan", "rowan")
    assert r.returncode == 0, r.stderr
    cfg = json.loads(r.stdout)
    assert cfg["name"] == "hermes-rowan"
    svc = cfg["services"]["hermes"]
    assert svc["container_name"] == "hermes-rowan"
    assert svc["command"] == ["gateway", "run"]
    homes = [v["source"] for v in svc["volumes"] if v["target"] == "/opt/data"]
    assert homes == [str(tmp_path / ".hermes-rowan")]


def test_uid_and_gid_have_no_default_and_fail_closed(tmp_path):
    """A wrong value re-owns an agent's live state in place, so absent must be fatal."""
    r = compose_config(tmp_path, tmp_path / ".hermes-rowan", "rowan",
                       extra_env={"HERMES_UID": "", "HERMES_GID": ""})
    assert r.returncode != 0
    assert "HERMES_UID" in r.stderr


def test_a_missing_home_is_fatal_rather_than_defaulted(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-rowan", "rowan", extra_env={"AGENT_HOME": ""})
    assert r.returncode != 0
    assert "AGENT_HOME" in r.stderr


def test_a_missing_image_is_fatal_rather_than_falling_back_to_a_tag(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-rowan", "rowan", extra_env={"AGENT_IMAGE": ""})
    assert r.returncode != 0
    assert "AGENT_IMAGE" in r.stderr


def test_no_port_is_published(tmp_path):
    """The dashboard holds API keys and Hermes refuses 0.0.0.0 without auth."""
    r = compose_config(tmp_path, tmp_path / ".hermes-rowan", "rowan")
    assert not json.loads(r.stdout)["services"]["hermes"].get("ports")


def test_no_credential_is_passed_through_compose(tmp_path):
    """The gateway reads /opt/data/.env through the mount; compose must carry none."""
    r = compose_config(tmp_path, tmp_path / ".hermes-rowan", "rowan")
    env = json.loads(r.stdout)["services"]["hermes"].get("environment", {})
    keys = set(env) if isinstance(env, dict) else {e.split("=")[0] for e in env}
    for forbidden in ("PLOW_CHAT_TOKEN", "DOMO_MCP_TOKEN", "HOSTEX_TOKEN", "SEAM_API_KEY"):
        assert forbidden not in keys


def test_the_template_mounts_nothing_but_the_agents_own_home(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-rowan", "rowan")
    svc = json.loads(r.stdout)["services"]["hermes"]
    assert [v["target"] for v in svc["volumes"]] == ["/opt/data"]


def test_an_instance_override_adds_a_build_and_merges_volumes(tmp_path):
    """This is what 'an agent inherits from agent-mgr' means concretely."""
    build_ctx = tmp_path / "str-repo"
    (build_ctx / "bin").mkdir(parents=True)
    (build_ctx / "Dockerfile").write_text("FROM scratch\n")
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    override = tmp_path / "compose.override.yml"
    override.write_text(
        "services:\n"
        "  hermes:\n"
        "    build: ${STR_REPO:?}\n"
        "    image: sams-str-hermes-agent:local\n"
        "    volumes:\n"
        "      - ${STR_VAULT:?}:/opt/data/repo/vault\n"
    )
    r = compose_config(tmp_path, tmp_path / ".hermes", "str", override=override,
                       extra_env={"STR_REPO": str(build_ctx), "STR_VAULT": str(vault)})
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]["hermes"]
    assert svc["build"]["context"] == str(build_ctx)
    assert svc["image"] == "sams-str-hermes-agent:local"
    assert {v["target"] for v in svc["volumes"]} == {"/opt/data", "/opt/data/repo/vault"}


def test_an_override_that_names_a_missing_variable_fails_loud(tmp_path):
    override = tmp_path / "compose.override.yml"
    override.write_text(
        "services:\n  hermes:\n    volumes:\n      - ${STR_VAULT:?}:/opt/data/repo/vault\n")
    r = compose_config(tmp_path, tmp_path / ".hermes", "str", override=override)
    assert r.returncode != 0
    assert "STR_VAULT" in r.stderr
