"""The converger, run as code. Tests import the real lib/cron-sync.py."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

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


def existing_job(**over):
    """A stored job with the fields the converger touches, in hermes's own
    shape -- confirmed against a live agent's jobs.json (2026-08-27)."""
    job = {
        "name": "seed",
        "enabled": True,
        "paused_at": None,
        "prompt": "",
        "script": None,
        "no_agent": False,
        "skills": [],
        "deliver": "local",
        "schedule": {"kind": "cron", "expr": "0 6 * * *", "display": "0 6 * * *"},
        "schedule_display": "0 6 * * *",
    }
    job.update(over)
    return job


def match_of(r):
    """The stored form of spec row r, healthy."""
    return existing_job(
        name=r["name"],
        prompt=r.get("prompt", ""),
        script=r.get("script"),
        no_agent=bool(r.get("no_agent")),
        skills=r.get("skills", []),
        deliver=r["deliver"],
        schedule_display=r["schedule"],
        schedule={"kind": "cron", "expr": r["schedule"], "display": r["schedule"]},
    )


@pytest.mark.parametrize(
    "rows,why",
    [
        ([row(deliver=None)], "deliver"),  # deliver is required, always
        ([row(prompt=None)], "prompt"),  # at least one of prompt/script
        ([row(name=None)], "name"),
        ([row(schedule=None)], "schedule"),
        (
            [row(no_agent=True)],
            "no_agent",
        ),  # no_agent means no agent turn, so a prompt is dead weight
        ([row(prompt=None, script="x.sh", no_agent=True, extra="?")], "unknown"),
        (
            [row(deliver="plow_chat:${NO_SUCH_UID}")],
            "NO_SUCH_UID",
        ),  # unset var refuses, never empty-expands
        ([row(deliver="plow_chat:$")], "deliver"),  # malformed placeholder refuses, not a traceback
        (
            [row(deliver="plow_chat:")],
            "blank",
        ),  # literal blank destination -- no ${} for the allowlist to catch
        ([row(deliver="plow_chat: ")], "blank"),
        ([row(), row()], "twice"),  # duplicate spec name
        (["oops"], "object"),  # a row must be an object
        ([["nested"]], "object"),
    ],
)
def test_load_spec_refuses(rows, why):
    with pytest.raises(SystemExit) as exc:
        cron_sync.load_spec(json.dumps(rows), env={})
    assert why in str(exc.value)


def test_a_script_can_feed_the_agents_prompt():
    """hermes's own contract: --script without --no-agent injects the script's
    stdout into the prompt each run. The live hostex-inbound job is this shape,
    so a spec that could not say it could not describe the fleet."""
    text = json.dumps([row(script="poll.py")])
    assert cron_sync.load_spec(text, env={})[0]["script"] == "poll.py"


@pytest.mark.parametrize("var", ["CHAT_UID", "PLOW_HOME_CHANNEL"])
def test_load_spec_expands_deliver_from_env(var):
    text = json.dumps([row(deliver="plow_chat:${%s}" % var)])
    rows = cron_sync.load_spec(text, env={var: "cht_abc"})
    assert rows[0]["deliver"] == "plow_chat:cht_abc"


@pytest.mark.parametrize(
    "deliver,env,why",
    [
        # The env source holds credentials beside delivery ids, and an expanded
        # deliver lands in hermes argv and persists verbatim in jobs.json -- so
        # only names ending _UID/_CHANNEL may be referenced, even when the var is
        # set. Both token generations, so the rename cannot re-admit either.
        ("plow_chat:${PLOW_CHAT_TOKEN}", {"PLOW_CHAT_TOKEN": "tok"}, "PLOW_CHAT_TOKEN"),
        ("plow_chat:${PLOW_AGENT_TOKEN}", {"PLOW_AGENT_TOKEN": "tok"}, "PLOW_AGENT_TOKEN"),
        # restore writes PLOW_HOME_CHANNEL= empty until activate runs; "plow_chat:"
        # is the silent-drop target this field exists to close.
        ("plow_chat:${CHAT_UID}", {"CHAT_UID": ""}, "empty"),
        ("plow_chat:${CHAT_UID}", {"CHAT_UID": "  "}, "empty"),
    ],
)
def test_deliver_expansion_is_allowlisted(deliver, env, why):
    with pytest.raises(SystemExit) as exc:
        cron_sync.load_spec(json.dumps([row(deliver=deliver)]), env=env)
    assert why in str(exc.value)


def test_load_spec_blocked_row_keeps_its_reason():
    text = json.dumps(
        [
            {
                "name": "j2",
                "schedule": "0 7 * * *",
                "prompt": "p",
                "deliver": "local",
                "blocked": "waiting on latch#183",
            }
        ]
    )
    assert cron_sync.load_spec(text, env={})[0]["blocked"] == "waiting on latch#183"


def test_registered_reads_hermes_own_state(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text(json.dumps({"jobs": [existing_job()], "updated_at": "x"}))
    jobs, dupes = cron_sync.registered(str(p))
    assert set(jobs) == {"seed"} and not dupes


def test_registered_absent_file_is_the_only_empty(tmp_path):
    assert cron_sync.registered(str(tmp_path / "nope.json")) == ({}, set())


@pytest.mark.parametrize(
    "content", ["not json", '{"no_jobs_key": []}', '{"jobs": [{"name": "x"}]}']
)  # missing enabled/paused_at
def test_registered_malformed_raises(tmp_path, content):
    p = tmp_path / "jobs.json"
    p.write_text(content)
    with pytest.raises(Exception):
        cron_sync.registered(str(p))


@pytest.mark.parametrize(
    "spec_name,stored_name,want_rc,want_calls,want_msg",
    [
        # Spec-named duplicate fails the run: hermes happily persists two jobs
        # under one name, and last-wins would print `ok` while the OTHER copy
        # still fires -- duplicate work, silently.
        ("held", "held", 1, 0, "duplicate"),
        # A duplicate among the agent's OWN crons is the agent's business -- the
        # spec never names it, and failing on it would block unrelated rows.
        ("fresh", "remind-me", 0, 1, None),
    ],
)
def test_duplicate_stored_names(
    monkeypatch, tmp_path, capsys, spec_name, stored_name, want_rc, want_calls, want_msg
):
    calls = _wire(
        monkeypatch,
        tmp_path,
        jobs=[match_of(row(name=stored_name)), match_of(row(name=stored_name))],
    )
    rc = cron_sync.main(["--spec-json", json.dumps([row(name=spec_name)])])
    assert (rc, len(calls)) == (want_rc, want_calls)
    assert want_msg is None or want_msg in capsys.readouterr().out


def test_absent_is_created_present_is_not():
    rows = [row(name="new"), row(name="old")]
    actions = dict(
        (r["name"], a) for a, r in cron_sync.classify(rows, {"old": match_of(row(name="old"))})
    )
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
    stored = match_of(r)
    stored["paused_at"] = "2026-08-01T00:00:00Z"
    assert cron_sync.classify([r], {"j1": stored}) == [("paused", r)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("deliver", "plow_chat:cht_other"),
        ("prompt", "different words"),
        ("skills", ["extra"]),
        ("schedule_display", "0 9 * * *"),
        ("script", "other.sh"),
        ("no_agent", True),
    ],
)
def test_drift_is_loud(field, value):
    r = row()
    stored = match_of(r)
    stored[field] = value
    if field == "schedule_display":
        stored["schedule"]["expr"] = stored["schedule"]["display"] = value
    assert cron_sync.classify([r], {"j1": stored}) == [("drifted", r)]


def test_blocked_absent_is_quiet_blocked_live_is_not():
    b = row(blocked="latch#183")
    assert cron_sync.classify([b], {}) == [("blocked", b)]
    assert cron_sync.classify([b], {"j1": match_of(row())}) == [("blocked-live", b)]


def test_foreign_jobs_are_invisible():
    agent_authored = match_of(row(name="remind-me"))
    assert cron_sync.classify([row()], {"remind-me": agent_authored, "j1": match_of(row())}) == [
        ("ok", row())
    ]


def test_create_argv_shapes():
    p = cron_sync.create_argv(row(skills=["ld-weather"]))
    assert p == [
        cron_sync.HERMES,
        "cron",
        "create",
        "0 6 * * *",
        "do it",
        "--name",
        "j1",
        "--deliver",
        "local",
        "--skill",
        "ld-weather",
    ]
    s = cron_sync.create_argv(
        {
            "name": "n",
            "schedule": "0 3 * * *",
            "script": "nightly.sh",
            "no_agent": True,
            "deliver": "local",
        }
    )
    assert s == [
        cron_sync.HERMES,
        "cron",
        "create",
        "0 3 * * *",
        "--name",
        "n",
        "--deliver",
        "local",
        "--script",
        "nightly.sh",
        "--no-agent",
    ]


def _wire(monkeypatch, tmp_path, jobs=None, env=None, rc=0):
    """Point the converger's three collaborators at test doubles.

    The production script has no override seams for these -- they are fixed
    paths inside the container by design -- so tests patch the module attrs.
    Returns the list create_argv calls land in.
    """
    jf = tmp_path / "jobs.json"
    if jobs is not None:
        jf.write_text(json.dumps({"jobs": jobs, "updated_at": "x"}))
    monkeypatch.setattr(cron_sync, "JOBS_FILE", str(jf))
    monkeypatch.setattr(cron_sync, "gateway_env", lambda: dict(env or {}))
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, rc)

    monkeypatch.setattr(cron_sync.subprocess, "run", run)
    return calls


def test_main_exit_codes(monkeypatch, tmp_path, capsys):
    calls = _wire(monkeypatch, tmp_path, jobs=[match_of(row(name="held"))])
    spec = json.dumps([row(name="fresh"), row(name="held")])
    rc = cron_sync.main(["--spec-json", spec])
    assert rc == 0 and len(calls) == 1 and "fresh" in " ".join(calls[0])
    held = match_of(row(name="held"))
    held["paused_at"] = "t"
    calls = _wire(monkeypatch, tmp_path, jobs=[match_of(row(name="fresh")), held])
    rc = cron_sync.main(["--spec-json", spec])
    assert rc == 1 and "paused" in capsys.readouterr().out


def test_main_expands_deliver_from_the_gateways_env(monkeypatch, tmp_path):
    """The env source is the gateway's own loader (hermes_cli.load_env) -- an
    exec session's env never carries the per-instance PLOW_* values,
    which is exactly the expansion this feature exists for."""
    calls = _wire(monkeypatch, tmp_path, env={"CHAT_UID": "cht_abc"})
    rc = cron_sync.main(["--spec-json", json.dumps([row(deliver="plow_chat:${CHAT_UID}")])])
    assert rc == 0
    assert "plow_chat:cht_abc" in calls[0]


def test_a_failed_create_fails_the_run(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path, rc=1)
    rc = cron_sync.main(["--spec-json", json.dumps([row()])])
    assert rc == 1 and "create failed" in capsys.readouterr().out
