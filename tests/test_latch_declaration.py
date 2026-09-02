"""Finding an agent's Latch server, and scaffolding it under the right key.

Both halves of one mistake: the detector matched a key name only this repo's
fixture used, and the scaffold wrote a key no shipped agent uses.
"""

from pathlib import Path

from agent_mgr.commands import config_declares_latch

ROOT = Path(__file__).resolve().parent.parent
RELAY_URL = "https://api.plow.co/v1/relay/devices/${DOMO_DEVICE_UID}/mcp"


def _config(tmp_path, body):
    f = tmp_path / "config.yaml"
    f.write_text(body)
    return f


def test_it_finds_the_relay_under_the_key_shipped_agents_use(tmp_path):
    """`plow`, which is what every real agent and the base image key it.

    This is the case that was broken: `set-latch` refused a working config
    with "declares no latch server", so an entrant following the quickstart
    could not connect at all.
    """
    assert config_declares_latch(
        _config(tmp_path, f"mcp_servers:\n  plow:\n    url: {RELAY_URL}\n")
    )


def test_it_still_finds_the_relay_under_the_old_key(tmp_path):
    """`latch`, which this repo's own fixture and scaffold used.

    Kept working deliberately: the key names the model's tools, so an operator
    who already has a `latch`-keyed agent must not be told it is unconfigured
    for choosing the name we used to hand them.
    """
    assert config_declares_latch(
        _config(tmp_path, f"mcp_servers:\n  latch:\n    url: {RELAY_URL}\n")
    )


def test_any_key_name_works_because_the_url_is_the_contract(tmp_path):
    assert config_declares_latch(
        _config(tmp_path, f"mcp_servers:\n  whatever:\n    url: {RELAY_URL}\n")
    )


def test_a_config_with_no_relay_is_not_a_latch_agent(tmp_path):
    """An agent that drives no Mac. `check-latch` must say so rather than probe."""
    assert not config_declares_latch(
        _config(tmp_path, "mcp_servers:\n  hostex:\n    url: https://api.hostex.io/mcp\n")
    )


def test_a_relay_url_outside_mcp_servers_does_not_count(tmp_path):
    """A URL in a comment or another block is not a declaration.

    The block scan is what keeps a mention from reading as configuration --
    `set-latch` would then write a credential nothing loads.
    """
    assert not config_declares_latch(
        _config(tmp_path, f"notes:\n  see: {RELAY_URL}\nmcp_servers:\n  hostex:\n    url: https://x/mcp\n")
    )


def test_the_scaffold_keys_the_relay_plow():
    """A new agent must get `mcp__plow__*`, like every shipped one.

    Keyed `latch`, a scaffolded agent registers its tools under a name the
    skills do not call -- and an unregistered tool is not an error, just a step
    that never runs.
    """
    scaffold = (ROOT / "templates" / "config.yaml").read_text()
    assert "\n  plow:\n" in scaffold
    assert "\n  latch:\n" not in scaffold


def test_the_scaffold_is_detected_by_its_own_detector():
    """The two halves agree: what we write is what we can find."""
    assert config_declares_latch(ROOT / "templates" / "config.yaml")
