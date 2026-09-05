import json
import os
import pytest
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST = "nousresearch/hermes-agent@sha256:" + "c" * 64
LEGACY_HOME = "/opt/data"
CURRENT_HOME = "/var/lib/hermes"


def compose_config(tmp_path, home, name, override=None, extra_env=None, contract="legacy"):
    """Resolve the template the way agent-mgr will, and return the CompletedProcess.

    The one place in the suite that runs the REAL docker, and it opts back in
    explicitly: `compose config` renders the merged file and never contacts the
    daemon, so it cannot start, stop or restart anything. Everything else runs
    with docker off PATH -- see conftest's `_no_real_docker_on_path`.

    The project names here are deliberately not the fleet's. `config` is inert,
    but a test naming a live project is one edit away from a subcommand that is
    not.

    `contract` picks which of agent-mgr's two boot-contract overlays gets
    layered in, the way compose_argv() would choose it from the image's own
    baked HERMES_HOME -- never a separate switch a test could get out of sync.
    """
    from conftest import REAL_PATH, allow_real_docker
    env = dict(os.environ)
    env["PATH"] = REAL_PATH
    env.update({
        "AGENT_HOME": str(home), "AGENT_CONTAINER": f"hermes-test-{name}",
        "AGENT_PROJECT": f"hermes-test-{name}", "AGENT_TZ": "America/Los_Angeles",
        "AGENT_IMAGE": DIGEST, "HERMES_UID": "1000", "HERMES_GID": "1000",
        # agent-mgr always resolves this (models.py environment()); the
        # template requires it so an unnamed agent cannot report anonymously.
        "AGENT_NAME": name,
        "AGENT_HOME_TARGET": CURRENT_HOME if contract == "current" else LEGACY_HOME,
    })
    if contract == "current":
        env["AGENT_CREDENTIALS_HOST"] = str(home.parent / "credentials.host")
    if extra_env:
        env.update(extra_env)
    contract_file = "compose.current.yml" if contract == "current" else "compose.legacy.yml"
    files = ["-f", str(ROOT / "templates" / "compose.yml")]
    if override:
        files += ["-f", str(override)]
    # Last, the way compose_argv() layers it.
    files += ["-f", str(ROOT / "templates" / contract_file)]
    with allow_real_docker():
        return subprocess.run(["docker", "compose", *files, "config", "--format", "json"],
                              capture_output=True, text=True, env=env)


def test_the_template_resolves_one_service_bound_to_the_agents_home(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    assert r.returncode == 0, r.stderr
    cfg = json.loads(r.stdout)
    assert cfg["name"] == "hermes-test-rowan"
    # Identity is the registry name, so two instances of one repo report as
    # two agents rather than collapsing into one row.
    assert cfg["services"]["hermes"]["environment"]["AGENT_ID"] == "rowan"
    svc = cfg["services"]["hermes"]
    assert svc["container_name"] == "hermes-test-rowan"
    assert svc["command"] == ["gateway", "run"]
    assert svc["environment"]["S6_SERVICES_GRACETIME"] == "30000"
    assert svc["stop_grace_period"] == "35s"
    homes = [v["source"] for v in svc["volumes"] if v["target"] == LEGACY_HOME]
    assert homes == [str(tmp_path / ".hermes-test-rowan")]


@pytest.mark.parametrize(("extra_env", "error"), [
    ({"HERMES_UID": "", "HERMES_GID": ""}, "HERMES_UID"),
    ({"AGENT_HOME": ""}, "AGENT_HOME"),
    ({"AGENT_IMAGE": ""}, "AGENT_IMAGE"),
    ({"AGENT_HOME_TARGET": ""}, "AGENT_HOME_TARGET"),
])
def test_required_compose_env_fails_closed(tmp_path, extra_env, error):
    """No defaults for any of the four. A wrong HERMES_UID re-owns an agent's
    live state in place, a defaulted home mounts the wrong directory, a
    fallback image tag re-resolves on the next pull, and a guessed mount
    target starts a gateway against an empty home."""
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan",
                       extra_env=extra_env)
    assert r.returncode != 0
    assert error in r.stderr


def test_no_port_is_published(tmp_path):
    """The dashboard holds API keys and Hermes refuses 0.0.0.0 without auth."""
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    assert not json.loads(r.stdout)["services"]["hermes"].get("ports")


def test_no_credential_is_passed_through_compose(tmp_path):
    """The gateway reads its home's own .env through the mount; compose must
    carry none."""
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    env = json.loads(r.stdout)["services"]["hermes"].get("environment", {})
    keys = set(env) if isinstance(env, dict) else {e.split("=")[0] for e in env}
    for forbidden in ("PLOW_AGENT_TOKEN", "PLOW_CHAT_TOKEN", "DOMO_MCP_TOKEN",
                      "HOSTEX_TOKEN", "SEAM_API_KEY"):
        assert forbidden not in keys


def test_the_template_mounts_nothing_but_the_agents_own_home(tmp_path):
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan")
    svc = json.loads(r.stdout)["services"]["hermes"]
    assert [v["target"] for v in svc["volumes"]] == [LEGACY_HOME]


def test_the_current_contract_starts_no_second_gateway_and_mounts_credentials(tmp_path):
    """The current base already boots a supervised gateway of its own; layering
    in the legacy entrypoint/command here would start a second one beside it.
    Through an override retargeting the credential bind at a sibling and
    dropping `:ro`, which the overlay -- merged after it -- simply overwrites."""
    override = tmp_path / "compose.override.yml"
    override.write_text(
        "services:\n"
        "  hermes:\n"
        "    volumes:\n"
        "      - /tmp/sibling-credentials:/var/lib/plow/credentials.host\n")
    r = compose_config(tmp_path, tmp_path / ".hermes-test-rowan", "rowan", contract="current",
                       override=override)
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]["hermes"]
    assert not svc.get("command")
    assert not svc.get("entrypoint")
    volumes = {v["target"]: v for v in svc["volumes"]}
    assert volumes[CURRENT_HOME]["source"] == str(tmp_path / ".hermes-test-rowan")
    credentials = volumes["/var/lib/plow/credentials.host"]
    assert credentials["source"] == str(tmp_path / "credentials.host")
    # Read-only in addition to living outside the agent's own home (asserted
    # in test_boot_contract.py) -- never write access to its own source copy.
    assert credentials["read_only"] is True


@pytest.mark.parametrize(("contract", "entrypoint", "command", "targets"), [
    ("legacy", ["/opt/hermes/docker/entrypoint-dispatch.sh"], ["gateway", "run"],
     {LEGACY_HOME, "/srv/vault"}),
    # The same hostile override through the contract whose overlay names no
    # boot chain of its own: merging last carries only what it DECLARES, so
    # a legacy entrypoint left un-reset survives the migration.
    ("current", None, None,
     {CURRENT_HOME, "/srv/vault", "/var/lib/plow/credentials.host"}),
])
def test_an_instance_override_merges_but_never_outranks_the_contract(
        tmp_path, contract, entrypoint, command, targets):
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
        # A bare tag here runs an image agent-mgr never inspected.
        "    image: sams-str-hermes-agent:local\n"
        "    volumes:\n"
        "      - ${STR_VAULT:?}:/srv/vault\n"
        # An override can replace anything the template set, identity
        # included -- which is why resolve_guard checks AGENT_ID after
        # the merge. A forged one attributes a person's usage to a
        # sibling, and says nothing while doing it.
        "    environment:\n"
        "      - AGENT_ID=someone-else\n"
        # ...except a key the CONTRACT overlay declares: merged last, it wins.
        "    entrypoint: [\"/bin/sh\"]\n"
        "    command: [\"-c\", \"sleep infinity\"]\n"
    )
    r = compose_config(tmp_path, tmp_path / ".hermes", "str", override=override,
                       contract=contract,
                       extra_env={"STR_REPO": str(build_ctx), "STR_VAULT": str(vault)})
    assert r.returncode == 0, r.stderr
    svc = json.loads(r.stdout)["services"]["hermes"]
    assert svc["build"]["context"] == str(build_ctx)
    assert svc["image"] == DIGEST, "compose would run an image agent-mgr never inspected"
    assert svc["environment"]["AGENT_ID"] == "someone-else", \
        "the override no longer wins; resolve_guard's premise is gone"
    assert {v["target"] for v in svc["volumes"]} == targets
    assert svc.get("entrypoint") == entrypoint
    assert svc.get("command") == command


def test_an_override_that_names_a_missing_variable_fails_loud(tmp_path):
    override = tmp_path / "compose.override.yml"
    override.write_text(
        "services:\n  hermes:\n    volumes:\n      - ${STR_VAULT:?}:/opt/data/repo/vault\n")
    r = compose_config(tmp_path, tmp_path / ".hermes", "str", override=override)
    assert r.returncode != 0
    assert "STR_VAULT" in r.stderr
