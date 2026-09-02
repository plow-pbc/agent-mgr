"""Finding an agent's Latch server, and scaffolding it under the right key.

Both halves of one mistake: the detector matched a key name only this repo's
fixture used, and the scaffold wrote a key no shipped agent uses.
"""

from pathlib import Path

import pytest

from agent_mgr.commands import config_declares_latch, latch_endpoint

ROOT = Path(__file__).resolve().parent.parent
RELAY_URL = "https://api.plow.co/v1/relay/devices/${DOMO_DEVICE_UID}/mcp"


def _config(tmp_path, body):
    f = tmp_path / "config.yaml"
    f.write_text(body)
    return f


@pytest.mark.parametrize(
    ("body", "found"),
    [
        # The case that was broken: `plow` is what every shipped agent and the
        # base image use, and matching the literal `latch:` refused them all.
        (f"mcp_servers:\n  plow:\n    url: {RELAY_URL}\n", True),
        # Kept working deliberately: an operator who took the name we used to
        # scaffold must not be told they are unconfigured for it.
        (f"mcp_servers:\n  latch:\n    url: {RELAY_URL}\n", True),
        (f"mcp_servers:\n  whatever:\n    url: {RELAY_URL}\n", True),
        # An agent that drives no Mac; check-latch must say so, not probe.
        ("mcp_servers:\n  hostex:\n    url: https://api.hostex.io/mcp\n", False),
        # A mention outside the block is not a declaration -- set-latch would
        # otherwise write a credential nothing loads.
        (f"notes:\n  see: {RELAY_URL}\nmcp_servers:\n  hostex:\n    url: https://x/mcp\n", False),
        # Hermes does not load a commented line, so neither does this.
        (f"mcp_servers:\n  plow:\n    # url: {RELAY_URL}\n", False),
    ],
)
def test_what_counts_as_a_declared_latch_server(tmp_path, body, found):
    """The URL is the contract, not the key name and not a mention of it."""
    assert config_declares_latch(_config(tmp_path, body)) is found


def test_it_returns_the_endpoint_the_config_names(tmp_path):
    """A boolean is what let the two halves disagree.

    The detector accepted a self-hosted relay while `check-latch` posted the
    credential to a hardcoded api.plow.co -- one relay's bearer sent to another
    host. Returning the endpoint is what makes both read the same fact.
    """
    body = "mcp_servers:\n  plow:\n    url: https://relay.example.test/v1/relay/devices/dev_1/mcp\n"
    assert latch_endpoint(_config(tmp_path, body)) == (
        "https://relay.example.test/v1/relay/devices/dev_1/mcp"
    )


def test_it_resolves_the_device_uid_from_the_dotenv(tmp_path):
    """The declared URL is a template; the probe needs the resolved one."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("DOMO_DEVICE_UID=dev_abc\nDOMO_MCP_TOKEN=t\n")
    cfg = _config(tmp_path, f"mcp_servers:\n  plow:\n    url: {RELAY_URL}\n")
    assert latch_endpoint(cfg, dotenv).endswith("/v1/relay/devices/dev_abc/mcp")


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
