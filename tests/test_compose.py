import json
import os
import pytest
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST = "nousresearch/hermes-agent@sha256:" + "c" * 64


def compose_config(tmp_path, home, name, override=None, extra_env=None):
    """Resolve the template the way agent-mgr will, and return the CompletedProcess.

    The one place in the suite that runs the REAL docker, and it opts back in
    explicitly: `compose config` renders the merged file and never contacts the
    daemon, so it cannot start, stop or restart anything. Everything else runs
    with docker off PATH -- see conftest's `_no_real_docker_on_path`.

    The project names here are deliberately not the fleet's. `config` is inert,
    but a test naming a live project is one edit away from a subcommand that is
    not.
    """
    from conftest import REAL_PATH, allow_real_docker
    env = dict(os.environ)
    env["PATH"] = REAL_PATH
    env.update({
        "AGENT_HOME": str(home), "AGENT_CONTAINER": f"hermes-test-{name}",
        "AGENT_PROJECT": f"hermes-test-{name}", "AGENT_TZ": "America/Los_Angeles",
        "AGENT_IMAGE": DIGEST, "HERMES_UID": "1000", "HERMES_GID": "1000",
    })
    if extra_env:
        env.update(extra_env)
    files = ["-f", str(ROOT / "templates" / "compose.yml")]
    if override:
        files += ["-f", str(override)]
    with allow_real_docker():
        return subprocess.run(["docker", "compose", *files, "config", "--format", "json"],
                              capture_output=True, text=True, env=env)


def test_the_template_resolves_one_service_bound_to_the_agents_home(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    assert r.returncode == 0, r.stderr
    cfg = json.loads(r.stdout)
    assert cfg["name"] == "hermes-test-rowan"
    svc = cfg["services"]["hermes"]
    assert svc["container_name"] == "hermes-test-rowan"
    assert svc["command"] == ["gateway", "run"]
    homes = [v["source"] for v in svc["volumes"] if v["target"] == "/opt/data"]
    assert homes == [str(tmp_path / ".hermes-test-rowan")]


@pytest.mark.parametrize(("extra_env", "error"), [
    ({"HERMES_UID": "", "HERMES_GID": ""}, "HERMES_UID"),
    ({"AGENT_HOME": ""}, "AGENT_HOME"),
    ({"AGENT_IMAGE": ""}, "AGENT_IMAGE"),
])
def test_required_compose_env_fails_closed(tmp_path, extra_env, error):
    """No defaults for any of the three. A wrong HERMES_UID re-owns an agent's
    live state in place, a defaulted home mounts the wrong directory, and a
    fallback image tag re-resolves on the next pull."""
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan",
                       extra_env=extra_env)
    assert r.returncode != 0
    assert error in r.stderr


def test_no_port_is_published(tmp_path):
    """The dashboard holds API keys and Hermes refuses 0.0.0.0 without auth."""
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    assert not json.loads(r.stdout)["services"]["hermes"].get("ports")


def test_no_credential_is_passed_through_compose(tmp_path):
    """The gateway reads /opt/data/.env through the mount; compose must carry none."""
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    env = json.loads(r.stdout)["services"]["hermes"].get("environment", {})
    keys = set(env) if isinstance(env, dict) else {e.split("=")[0] for e in env}
    for forbidden in ("PLOW_AGENT_TOKEN", "PLOW_CHAT_TOKEN", "DOMO_MCP_TOKEN",
                      "HOSTEX_TOKEN", "SEAM_API_KEY"):
        assert forbidden not in keys


def test_the_template_mounts_nothing_but_the_agents_own_home(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    svc = json.loads(r.stdout)["services"]["hermes"]
    assert [v["target"] for v in svc["volumes"]] == ["/opt/data"]


def test_container_shutdown_gives_the_gateway_time_to_release_session_locks(tmp_path):
    """s6's three-second default killed Hermes before shutdown cleanup finished.

    Docker must wait longer than the s6 service grace window, or a restart can
    strand an active compression lease and reject transcript writes until its
    TTL expires.
    """
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]["hermes"]

    assert svc["environment"]["S6_SERVICES_GRACETIME"] == "30000"
    assert svc["stop_grace_period"] == "35s"


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
