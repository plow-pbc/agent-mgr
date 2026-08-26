import os

import pytest
from conftest import fake_docker


def _fake_docker(tmp_path, name="rowan"):
    log = tmp_path / "argv.log"
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, log=log)
    return b, log


def test_sign_in_authenticates_against_the_installed_config_not_the_repo_copy(run, instance, tmp_path):
    """The gateway resolved model.provider from the INSTALLED config at boot.
    Reading the repo copy would mint a credential for a provider it is not using
    the moment the two differ."""
    run("register", "rowan", str(instance("rowan", config="model:\n  provider: openai-codex\n")))
    run("restore", "rowan")
    (tmp_path / "home" / ".hermes-rowan" / "config.yaml").write_text("model:\n  provider: anthropic\n")
    b, log = _fake_docker(tmp_path)
    r = run("sign-in", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "auth add anthropic" in calls
    assert "auth add openai-codex" not in calls


def test_sign_in_refuses_before_restore_has_run(run, instance):
    run("register", "rowan", str(instance("rowan")))
    r = run("sign-in", "rowan")
    assert r.returncode != 0
    assert "restore" in r.stderr


@pytest.mark.parametrize("command", ["activate", "set-latch"])
@pytest.mark.parametrize(
    "descriptor",
    ["AGENT_HOME=/etc\n", "AGENT_HOME=/tmp/.hermes-property\n"],
    ids=["not-a-hermes-home", "a-siblings-conventional-home"],
)
def test_credential_writers_refuse_a_home_that_is_not_this_agents(run, instance, command, descriptor):
    """Every command that writes a credential into a home takes the same guard.
    Pointed at a sibling's, activate would take that agent off its chat and spend
    a one-time activation; set-latch would hand it a relay credential minted
    against someone else's Mac."""
    run("register", "rowan", str(instance("rowan", descriptor=descriptor)))
    r = run(command, "rowan", input="dev_abc\ntok_xyz\n")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_activate_allows_a_legacy_bare_home_the_descriptor_declared(run, instance, tmp_path):
    """The rentals agent predates the ~/.hermes-<name> convention. An explicit
    declaration is deliberate; the convention can never produce a bare .hermes."""
    legacy = tmp_path / "home" / ".hermes"
    legacy.mkdir(parents=True)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    # ACTIVATE_REF, not PLUGIN_REF. activate reads its own pin now, so the
    # plugin override stopped reaching it -- the command ran on through curl,
    # bash and reload-if-running, which also dragged a hermetic test onto the
    # host's real docker daemon. And the surviving assertion matched a string
    # that appears nowhere in the tool, so it could not have failed either way.
    r = run("activate", "str", env={"AGENT_MGR_ACTIVATE_REF": "not-a-sha"})
    # It gets past the home guard and fails later, on the ref -- which is the
    # proof that the guard let it through. Asserted on what the tool prints.
    assert r.returncode != 0
    assert "40-char SHA" in r.stderr
    # The string the guard actually prints. Two other tests in this file pin the
    # same one; the previous two spellings here matched nothing any code emits,
    # so the line could not fail either way.
    assert "refusing to write" not in r.stderr


LATCH_CONFIG = (
    "model:\n  provider: openai-codex\n"
    "mcp_servers:\n  latch:\n    url: https://api.plow.co/v1/relay/devices/${DOMO_DEVICE_UID}/mcp\n"
)


@pytest.mark.parametrize(
    "starting_dotenv",
    [
        "HOSTEX_TOKEN=keep-me\nDOMO_DEVICE_UID=\nDOMO_MCP_TOKEN=\n",
        # No DOMO_* at all -- the append arm. An agent whose own .env.example
        # omits them starts here, and nothing else in the suite reaches it.
        "HOSTEX_TOKEN=keep-me\n",
        # The spellings a hand-edited file arrives in, which is how every one of
        # these was written before this command existed. Each must be replaced
        # in place, not left underneath an appended second declaration.
        "HOSTEX_TOKEN=keep-me\nexport DOMO_DEVICE_UID=stale\n  DOMO_MCP_TOKEN = stale\n",
        # Already duplicated. The upsert collapses it rather than adding a third.
        "HOSTEX_TOKEN=keep-me\nDOMO_MCP_TOKEN=stale\nexport DOMO_MCP_TOKEN=staler\n",
    ],
    ids=["pre-seeded-empty", "absent", "hand-edited-spellings", "already-duplicated"],
)
def test_set_latch_writes_the_pair_and_carries_every_other_key_through(run, instance, tmp_path, starting_dotenv):
    """The dotenv is shared -- the rentals agent keeps a PMS token and a lock API
    key in the same file -- so an upsert that rewrote the file would take those
    with it. And whatever spelling a key arrives in, exactly one declaration may
    survive: two readers of this file disagree about which of a pair is live."""
    run("register", "rowan", str(instance("rowan", config=LATCH_CONFIG)))
    run("restore", "rowan")
    env_file = tmp_path / "home" / ".hermes-rowan" / ".env"
    env_file.write_text(starting_dotenv)
    b, _ = _fake_docker(tmp_path)
    r = run("set-latch", "rowan", input="dev_abc\ntok_xyz\n",
            env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    body = env_file.read_text()
    # The whole LINE, at column 0 -- not a substring. A future edit that carried
    # the incoming line's `export ` or indent through would satisfy a substring
    # match and produce exactly the spelling the readers have to agree about.
    assert "DOMO_DEVICE_UID=dev_abc" in body.splitlines()
    assert "DOMO_MCP_TOKEN=tok_xyz" in body.splitlines()
    assert "HOSTEX_TOKEN=keep-me" in body.splitlines()
    # One declaration each, in any spelling -- not a second appended beside the
    # one it was meant to replace, and no stale value left underneath.
    assert body.count("DOMO_MCP_TOKEN=") == 1
    assert body.count("DOMO_DEVICE_UID=") == 1
    assert "stale" not in body
    # The dotenv holds live credentials and the home is on a shared host.
    assert (env_file.stat().st_mode & 0o777) == 0o600
    # Never the whole token, on either stream -- the operator may be screen-sharing.
    assert "tok_xyz" not in r.stdout
    assert "tok_xyz" not in r.stderr


def test_set_latch_refuses_an_agent_whose_config_declares_no_latch(run, instance):
    """A pair written for an agent with no latch is a credential no gateway ever
    reads, sitting in a dotenv looking like working configuration."""
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    r = run("set-latch", "rowan", input="dev_abc\ntok_xyz\n")
    assert r.returncode != 0
    assert "declares no latch" in r.stderr


def test_set_latch_refuses_an_empty_value_rather_than_writing_it(run, instance, tmp_path):
    """An empty write is the failure check-latch already reports as 'mint one
    from the Mac'; refusing here keeps it from reaching the dotenv at all."""
    run("register", "rowan", str(instance("rowan", config=LATCH_CONFIG)))
    run("restore", "rowan")
    r = run("set-latch", "rowan", input="dev_abc\n\n")
    assert r.returncode != 0
    assert "DOMO_MCP_TOKEN was empty" in r.stderr
    assert "DOMO_DEVICE_UID=dev_abc" not in (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
