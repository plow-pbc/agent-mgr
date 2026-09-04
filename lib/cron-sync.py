#!/usr/bin/env python3
"""Converge a declarative cron spec onto Hermes' scheduler, idempotently.

Runs INSIDE the agent's container -- the one place `hermes`, jobs.json and the
jobs' environment all live. Piped over stdin by `agent-mgr cron-sync <name>`;
the spec travels as --spec-json. Nothing is installed, so nothing goes stale.

Ground truth is $HOME/cron/jobs.json -- hermes's own persisted state, where
a name is a field and so are enabled/paused_at. Never the human rendering of
`hermes cron list`: matching on that text needed a new guard every time it was
wrong (life-assistant's register_crons.py records the three review rounds).

The one invariant with teeth, carried from that script: only FileNotFoundError
means "nothing registered". Every other failure raises and stops the run --
"I could not tell what is registered" read as "nothing is" duplicates every job.

v1 converges by creation only. Paused, drifted, or blocked-but-live jobs are
reported loudly and fail the run; the operator resolves them with the exact
command the report names. Auto-edit waits until drift first happens in practice.
Jobs the spec does not name -- the agent's own crons -- are invisible here.
"""
import argparse
import json
import os
import pathlib
import string
import subprocess
import sys

HERMES = "/opt/hermes/bin/hermes"
JOBS_FILE = str(pathlib.Path.home() / "cron/jobs.json")

ROW_KEYS = {"name", "schedule", "prompt", "script", "no_agent", "skills",
            "deliver", "blocked"}


def load_spec(text, env):
    """Validated spec rows, with ${VAR} in deliver expanded from env."""
    try:
        rows = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"spec is not valid JSON: {exc}")
    if not isinstance(rows, list) or not rows:
        raise SystemExit("spec must be a non-empty JSON list of job rows")
    seen = set()
    for r in rows:
        if not isinstance(r, dict):
            raise SystemExit(f"spec row must be an object, got {type(r).__name__}")
        unknown = set(r) - ROW_KEYS
        if unknown:
            raise SystemExit(f"row {r.get('name')!r}: unknown keys {sorted(unknown)}")
        for k in ("name", "schedule", "deliver"):
            if not r.get(k):
                raise SystemExit(f"row {r.get('name')!r}: {k} is required -- "
                                 "deliver is never defaulted: an implicit one is "
                                 "the silent-drop trap this spec exists to close")
        if r["name"] in seen:
            raise SystemExit(f"row {r['name']!r} appears twice")
        seen.add(r["name"])
        # prompt and script are each a job's substance, and hermes accepts all
        # three of their live combinations: prompt alone (an agent turn), script
        # alone with no_agent (script-is-the-job), and script WITH a prompt --
        # the script's stdout feeds the agent's turn, which is what the live
        # hostex-inbound job is. Only the empty row and a prompt no one would
        # read (no_agent skips the LLM entirely) are refusals.
        if not r.get("prompt") and not r.get("script"):
            raise SystemExit(f"row {r['name']!r}: at least one of prompt/script")
        if r.get("no_agent"):
            if not r.get("script"):
                raise SystemExit(f"row {r['name']!r}: no_agent requires script")
            if r.get("prompt"):
                raise SystemExit(f"row {r['name']!r}: no_agent skips the agent, "
                                 "so its prompt would never be read")
        # Expand ${VAR} now, loudly. The env source holds credentials beside
        # delivery ids (PLOW_AGENT_TOKEN one line under PLOW_HOME_CHANNEL), and
        # an expanded deliver lands in hermes argv AND is persisted verbatim in
        # jobs.json -- so only delivery identifiers, names ending in _UID or
        # _CHANNEL, may be referenced at all. A blank resolved value refuses
        # too: deploy writes PLOW_HOME_CHANNEL= empty until activate runs, and
        # "plow_chat:" is the silent-drop target this field exists to close.
        tmpl = string.Template(r["deliver"])
        if not tmpl.is_valid():
            raise SystemExit(f"row {r['name']!r}: malformed ${{...}} in deliver")
        for n in tmpl.get_identifiers():
            if not n.endswith(("_UID", "_CHANNEL")):
                raise SystemExit(f"row {r['name']!r}: deliver may only reference "
                                 f"delivery identifiers (names ending _UID or _CHANNEL), not {n!r}")
            if not env.get(n, "").strip():
                raise SystemExit(f"row {r['name']!r}: deliver names {n}, "
                                 "which is unset or empty in this container")
        r["deliver"] = tmpl.substitute(env)
        # A literal "plow_chat:" carries no ${...} to check, so catch the
        # blank destination itself, post-substitution.
        if ":" in r["deliver"] and not r["deliver"].split(":", 1)[1].strip():
            raise SystemExit(f"row {r['name']!r}: deliver {r['deliver']!r} has a "
                             "blank destination -- the silent-drop target")
    return rows


def registered(jobs_path=JOBS_FILE):
    """({name: raw job dict}, duplicate names) from hermes's persisted state.

    hermes happily persists two jobs under one name. Duplicates are RETURNED,
    not refused here: only a spec-named duplicate fails the run (last-wins
    would print `ok` while the other copy still fires), while a duplicate
    among the agent's own crons stays the agent's business -- the spec never
    names it, and the invisibility contract in the module docstring holds.
    """
    try:
        jobs = json.loads(pathlib.Path(jobs_path).read_text())["jobs"]
    except FileNotFoundError:
        return {}, set()   # a fresh instance -- the ONLY absence that means empty
    out, dupes = {}, set()
    for job in jobs:
        # Subscript, not .get: every entry hermes writes carries these
        # (confirmed against a live agent's jobs.json, 2026-08-27). A default
        # would be semantics for a shape that does not occur -- and the wrong
        # ones if it ever did.
        job["enabled"], job["paused_at"]
        if job["name"] in out:
            dupes.add(job["name"])
        out[job["name"]] = job
    return out, dupes


def gateway_env():
    """The gateway's own env, through the gateway's own loader.

    A `docker exec` session sees the container's CONFIG env -- image ENV plus
    compose `environment:` -- never the gateway's runtime env: the per-instance
    values deliver expansion exists for (PLOW_HOME_CHANNEL and friends) live in the
    home's .env, which the gateway loads itself at boot. hermes_cli ships the
    loader that does that load, so use it rather than reimplement its grammar;
    this is why agent-mgr runs this script under /opt/hermes/.venv/bin/python3
    with HOME set to the home mount. Imported here, not at module top: the
    contract suite imports this module on a host with no hermes installed.
    """
    from hermes_cli.config import load_env
    return load_env()


def _stored_schedule_matches(row_schedule, job):
    # Accept any of hermes's renderings: `create 'every 2m'` is stored as
    # {kind: interval, minutes: 2} with only a display -- no expr at all --
    # so exact-match against ONE field would false-drift every interval job.
    # A miss is only ever a loud complaint, never a duplicate.
    sched = job["schedule"]
    return row_schedule in {sched.get("expr"), sched["display"], job["schedule_display"]}


def _drifted(row, job):
    return not (
        _stored_schedule_matches(row["schedule"], job)
        and job["prompt"] == row.get("prompt", "")
        and job["script"] == row.get("script")
        and bool(job["no_agent"]) == bool(row.get("no_agent"))
        and job["skills"] == row.get("skills", [])
        and job["deliver"] == row["deliver"]
    )


def classify(rows, existing):
    out = []
    for r in rows:
        job = existing.get(r["name"])
        if r.get("blocked"):
            out.append(("blocked-live" if job else "blocked", r))
        elif job is None:
            out.append(("create", r))
        elif job["paused_at"] or not job["enabled"]:
            out.append(("paused", r))
        elif _drifted(r, job):
            out.append(("drifted", r))
        else:
            out.append(("ok", r))
    return out


def create_argv(row):
    argv = [HERMES, "cron", "create", row["schedule"]]
    if row.get("prompt"):
        argv.append(row["prompt"])
    argv += ["--name", row["name"], "--deliver", row["deliver"]]
    for s in row.get("skills", []):
        argv += ["--skill", s]
    if row.get("script"):
        argv += ["--script", row["script"]]
    if row.get("no_agent"):
        argv.append("--no-agent")
    return argv


REFUSALS = {
    "paused": "registered but will never fire -- re-creating would duplicate it, "
              "skipping hides a job that stopped. Decide: hermes cron resume {name}, "
              "or remove the row",
    "drifted": "live job differs from the spec. v1 does not auto-edit; inspect with "
               "hermes cron list and reconcile with hermes cron edit {name} (or "
               "remove + re-run)",
    "blocked-live": "spec marks this blocked ({blocked}) but a live job exists -- "
                    "remove it or unblock the row",
}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec-json", required=True)
    args = ap.parse_args(argv)
    # The gateway's dotenv over the session env: the gateway's own values win,
    # and the session env still answers for anything compose set (TZ and friends).
    rows = load_spec(args.spec_json, env={**os.environ, **gateway_env()})
    existing, dupes = registered(JOBS_FILE)
    failed = False
    for action, r in classify(rows, existing):
        if r["name"] in dupes:
            print(f"REFUSED {r['name']} (duplicate): jobs.json holds more than "
                  "one job under this name -- remove the extra "
                  "(hermes cron remove) before syncing")
            failed = True
        elif action == "create":
            # The create's own echo is the confirmation; not captured.
            res = subprocess.run(create_argv(r))
            if res.returncode != 0:
                print(f"create failed for {r['name']} (exit {res.returncode})")
                failed = True
            else:
                print(f"created {r['name']}")
        elif action == "ok":
            print(f"ok {r['name']}")
        elif action == "blocked":
            print(f"blocked {r['name']}: {r['blocked']} (not registered, by design)")
        else:
            print(f"REFUSED {r['name']} ({action}): " + REFUSALS[action].format(**r))
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
