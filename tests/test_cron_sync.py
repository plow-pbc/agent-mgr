"""The converger, run as code. Tests import the real lib/cron-sync.py."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hermes-cron-jobs.json"

# No bytecode: importing the real script must not drop a __pycache__ into lib/,
# where the tests that walk lib/'s scripts would trip over the directory.
sys.dont_write_bytecode = True
spec_mod = importlib.util.spec_from_file_location("cron_sync", ROOT / "lib" / "cron-sync.py")
cron_sync = importlib.util.module_from_spec(spec_mod)
spec_mod.loader.exec_module(cron_sync)


def row(**over):
    base = {"name": "j1", "schedule": "0 6 * * *", "prompt": "do it", "deliver": "local"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize("bad,why", [
    (row(deliver=None), "deliver"),                    # deliver is required, always
    (row(prompt=None), "prompt"),                      # at least one of prompt/script
    (row(name=None), "name"),
    (row(schedule=None), "schedule"),
    (row(no_agent=True), "no_agent"),                  # no_agent means no agent turn, so a prompt is dead weight
    (row(prompt=None, script="x.sh", no_agent=True, extra="?"), "unknown"),
    (row(deliver="plow_chat:${NO_SUCH_VAR}"), "NO_SUCH_VAR"),  # unset var refuses, never empty-expands
])
def test_load_spec_refuses(bad, why):
    with pytest.raises(SystemExit) as exc:
        cron_sync.load_spec(json.dumps([bad]), env={})
    assert why in str(exc.value)


def test_load_spec_refuses_a_duplicate_name():
    with pytest.raises(SystemExit) as exc:
        cron_sync.load_spec(json.dumps([row(), row()]), env={})
    assert "twice" in str(exc.value)


def test_a_script_can_feed_the_agents_prompt():
    """hermes's own contract: --script without --no-agent injects the script's
    stdout into the prompt each run. The live hostex-inbound job is this shape,
    so a spec that could not say it could not describe the fleet."""
    text = json.dumps([row(script="poll.py")])
    assert cron_sync.load_spec(text, env={})[0]["script"] == "poll.py"


def test_load_spec_expands_deliver_from_env():
    text = json.dumps([row(deliver="plow_chat:${CHAT}")])
    rows = cron_sync.load_spec(text, env={"CHAT": "cht_abc"})
    assert rows[0]["deliver"] == "plow_chat:cht_abc"


def test_load_spec_blocked_row_keeps_its_reason():
    text = json.dumps([{"name": "j2", "schedule": "0 7 * * *", "prompt": "p",
                        "deliver": "local", "blocked": "waiting on latch#183"}])
    assert cron_sync.load_spec(text, env={})[0]["blocked"] == "waiting on latch#183"


def test_registered_reads_hermes_own_state():
    jobs = cron_sync.registered(str(FIXTURE))
    assert jobs  # the captured file has entries
    j = next(iter(jobs.values()))
    assert {"enabled", "paused_at", "deliver"} <= set(j)


def test_registered_absent_file_is_the_only_empty(tmp_path):
    assert cron_sync.registered(str(tmp_path / "nope.json")) == {}


@pytest.mark.parametrize("content", ["not json", '{"no_jobs_key": []}',
                                     '{"jobs": [{"name": "x"}]}'])  # missing enabled/paused_at
def test_registered_malformed_raises(tmp_path, content):
    p = tmp_path / "jobs.json"
    p.write_text(content)
    with pytest.raises(Exception):
        cron_sync.registered(str(p))
