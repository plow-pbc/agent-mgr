#!/usr/bin/env python3
"""Converge a declarative cron spec onto Hermes' scheduler, idempotently.

Runs INSIDE the agent's container -- the one place `hermes`, jobs.json and the
jobs' environment all live. Piped over stdin by `agent-mgr cron-sync <name>`;
the spec travels as --spec-json. Nothing is installed, so nothing goes stale.

Ground truth is /opt/data/cron/jobs.json -- hermes's own persisted state, where
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
JOBS_FILE = "/opt/data/cron/jobs.json"

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
        # Expand ${VAR} now, loudly. string.Template.substitute raises KeyError
        # on an unset name, which is the refusal -- an empty expansion would
        # register a job whose delivery target is garbage, silently.
        try:
            r["deliver"] = string.Template(r["deliver"]).substitute(env)
        except KeyError as exc:
            raise SystemExit(f"row {r['name']!r}: deliver names {exc} "
                             "which is unset in this container")
    return rows


def registered(jobs_path=JOBS_FILE):
    """{name: raw job dict} from hermes's own persisted state."""
    try:
        jobs = json.loads(pathlib.Path(jobs_path).read_text())["jobs"]
    except FileNotFoundError:
        return {}          # a fresh instance -- the ONLY absence that means empty
    out = {}
    for job in jobs:
        # Subscript, not .get: every entry hermes writes carries these
        # (pinned by tests/fixtures/hermes-cron-jobs.json, captured live).
        # A default would be semantics for a shape that does not occur --
        # and the wrong ones if it ever did.
        job["enabled"], job["paused_at"]
        out[job["name"]] = job
    return out
