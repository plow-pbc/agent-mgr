"""The converger, run as code. Tests import the real lib/cron-sync.py."""
import importlib.util
import json
import subprocess
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


def existing_job(**over):
    """A stored job in hermes's own shape, from the fixture's first entry."""
    j = json.loads(FIXTURE.read_text())["jobs"][0].copy()
    j.update(over)
    return j


def match_of(r):
    """The stored form of spec row r, healthy."""
    return existing_job(name=r["name"], prompt=r.get("prompt", ""),
                        script=r.get("script"), no_agent=bool(r.get("no_agent")),
                        skills=r.get("skills", []), deliver=r["deliver"],
                        schedule_display=r["schedule"],
                        schedule={"kind": "cron", "expr": r["schedule"],
                                  "display": r["schedule"]},
                        enabled=True, paused_at=None)


def test_absent_is_created_present_is_not():
    rows = [row(name="new"), row(name="old")]
    actions = dict((r["name"], a) for a, r in
                   cron_sync.classify(rows, {"old": match_of(row(name="old"))}))
    assert actions == {"new": "create", "old": "ok"}


def test_an_interval_schedule_matches_its_display_form():
    """hermes stores `every 2m` as {kind: interval, minutes: 2} -- no expr at
    all -- so a converger reading only expr would false-drift every interval
    job on the fleet (both live str jobs were the two kinds)."""
    r = row(schedule="every 2m")
    stored = match_of(r)
    stored["schedule"] = {"kind": "interval", "minutes": 2, "display": "every 2m"}
    stored["schedule_display"] = "every 2m"
    assert cron_sync.classify([r], {"j1": stored}) == [("ok", r)]


def test_paused_refuses_rather_than_duplicate_or_skip():
    r = row()
    stored = match_of(r); stored["paused_at"] = "2026-08-01T00:00:00Z"
    assert cron_sync.classify([r], {"j1": stored}) == [("paused", r)]


@pytest.mark.parametrize("field,value", [
    ("deliver", "plow_chat:cht_other"),
    ("prompt", "different words"),
    ("skills", ["extra"]),
    ("schedule_display", "0 9 * * *"),
])
def test_drift_is_loud(field, value):
    r = row()
    stored = match_of(r); stored[field] = value
    if field == "schedule_display":
        stored["schedule"]["expr"] = stored["schedule"]["display"] = value
    assert cron_sync.classify([r], {"j1": stored}) == [("drifted", r)]


def test_blocked_absent_is_quiet_blocked_live_is_not():
    b = row(blocked="latch#183")
    assert cron_sync.classify([b], {}) == [("blocked", b)]
    assert cron_sync.classify([b], {"j1": match_of(row())}) == [("blocked-live", b)]


def test_foreign_jobs_are_invisible():
    agent_authored = match_of(row(name="remind-me"))
    assert cron_sync.classify([row()], {"remind-me": agent_authored, "j1": match_of(row())}) \
        == [("ok", row())]


def test_create_argv_shapes():
    p = cron_sync.create_argv(row(skills=["ld-weather"]))
    assert p == [cron_sync.HERMES, "cron", "create", "0 6 * * *", "do it",
                 "--name", "j1", "--deliver", "local", "--skill", "ld-weather"]
    s = cron_sync.create_argv({"name": "n", "schedule": "0 3 * * *",
                               "script": "nightly.sh", "no_agent": True,
                               "deliver": "local"})
    assert s == [cron_sync.HERMES, "cron", "create", "0 3 * * *",
                 "--name", "n", "--deliver", "local",
                 "--script", "nightly.sh", "--no-agent"]


def test_main_exit_codes(tmp_path, capsys):
    jobs = {"jobs": [match_of(row(name="held"))], "updated_at": "x"}
    jf = tmp_path / "jobs.json"; jf.write_text(json.dumps(jobs))
    calls = []
    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    spec = json.dumps([row(name="fresh"), row(name="held")])
    rc = cron_sync.main(["--spec-json", spec, "--jobs-file", str(jf)], runner=runner)
    assert rc == 0 and len(calls) == 1 and "fresh" in " ".join(calls[0])
    held = json.loads(jf.read_text()); held["jobs"][0]["paused_at"] = "t"
    jf.write_text(json.dumps(held))
    rc = cron_sync.main(["--spec-json", spec, "--jobs-file", str(jf)], runner=runner)
    assert rc == 1 and "paused" in capsys.readouterr().out


def test_main_expands_deliver_from_the_gateways_dotenv(tmp_path):
    """The env source is /opt/data/.env, the file the GATEWAY loads -- an exec
    session's own env never carries the per-instance PLOW_CHAT_* values, which
    is exactly the expansion this feature exists for."""
    dotenv = tmp_path / ".env"
    # An indented line reads as ABSENT, exactly as common.sh's dotenv_read
    # reads it: one grammar, KEY=value at column 0, and the two tools must
    # not disagree about the same file.
    dotenv.write_text("# creds\nCHAT=cht_stale\nCHAT=cht_abc\n  CHAT=cht_indented\n")
    calls = []
    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)
    spec = json.dumps([row(deliver="plow_chat:${CHAT}")])
    rc = cron_sync.main(["--spec-json", spec, "--jobs-file", str(tmp_path / "nope.json"),
                         "--dotenv", str(dotenv)], runner=runner)
    assert rc == 0
    assert "plow_chat:cht_abc" in calls[0]  # last-wins, like the gateway
