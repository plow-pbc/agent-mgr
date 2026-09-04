import io
import json
import subprocess

import pytest
from conftest import ROOT

# The one pin the fleet runs; `resolve` must report it, not a copy of it.
FLEET_IMAGE = json.loads((ROOT / "runtime" / "stack.json").read_text())["images"]["hermes_local"]["reference"]


def payload(result):
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_resolve_json_is_a_versioned_typed_object(run, instance):
    repo = instance("rowan", "AGENT_TZ=America/Chicago\n")
    run("register", "rowan", str(repo), check=True)

    result = run("--json", "resolve", "rowan")

    assert result.returncode == 0
    body = payload(result)
    assert body == {
        "ok": True,
        "schema_version": 1,
        "operation": "resolve",
        "result": {
            "name": "rowan",
            "repo": str(repo),
            "home": str(repo.parent / "home" / ".hermes-rowan"),
            "container": "hermes-rowan",
            "project": "hermes-rowan",
            "timezone": "America/Chicago",
            "image": FLEET_IMAGE,
            "config": str(repo / "config.yaml"),
            "live": False,
            "transition_confirmation_required": False,
            "deploy_hook": None,
            "pre_transition_hook": None,
            "cron_spec": None,
            "boot_contract": "",
        },
    }


def test_json_failure_has_a_stable_code_and_no_human_prefix(run):
    result = run("--json", "resolve", "missing")

    assert result.returncode == 1
    assert result.stderr == ""
    body = json.loads(result.stdout)
    assert body["ok"] is False
    assert body["schema_version"] == 1
    assert body["operation"] == "resolve"
    assert body["error"]["code"] == "agent_not_found"
    assert body["error"]["message"] == "missing is not registered"
    assert body["error"]["remediation"] == "register the agent repository first"


def test_operational_json_failure_preserves_status_and_both_streams(run):
    result = run("--json", "resolve-guard", "missing")

    assert result.returncode == 1
    assert result.stderr == ""
    body = json.loads(result.stdout)
    assert body["error"]["code"] == "operation_failed"
    assert body["error"]["exit_code"] == 1
    assert body["error"]["stdout"] == []
    assert body["error"]["stderr"] == ["agent-mgr: missing is not registered"]


@pytest.mark.parametrize("terminal, expected", [(True, subprocess.DEVNULL), (False, None)])
def test_operational_json_detaches_only_terminal_stdin(monkeypatch, terminal, expected):
    monkeypatch.syspath_prepend(str(ROOT))
    from agent_mgr import cli

    stdin = io.StringIO("piped input\n")
    monkeypatch.setattr(stdin, "isatty", lambda: terminal)
    monkeypatch.setattr(cli.sys, "stdin", stdin)
    seen = {}

    def completed(*_args, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", completed)

    assert cli.main(["--json", "check-connectors", "rowan"]) == 0
    assert seen["stdin"] is expected


def test_json_registry_listing_is_data_not_a_table(run, instance):
    zed = instance("zed")
    amy = instance("amy")
    run("register", "zed", str(zed), check=True)
    run("register", "amy", str(amy), check=True)

    result = run("--json", "ls")

    assert result.returncode == 0
    assert payload(result)["result"] == {
        "agents": [
            {"name": "amy", "repo": str(amy)},
            {"name": "zed", "repo": str(zed)},
        ]
    }


def test_json_flag_is_accepted_after_the_operation(run):
    result = run("ls", "--json")

    assert result.returncode == 0
    assert payload(result)["operation"] == "ls"


def test_every_operation_has_one_json_document_on_stdout(run, instance, tmp_path):
    repo = instance("rowan")
    run("register", "rowan", str(repo), check=True)

    result = run("--json", "resolve-guard", "rowan")

    assert result.returncode == 0
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["operation"] == "resolve-guard"
    assert body["result"]["exit_code"] == 0
    assert body["result"]["stdout"] == []
    assert result.stderr == ""


@pytest.mark.parametrize("arguments", [("logs", "rowan"), ("compose", "rowan", "logs", "-f")])
def test_json_rejects_unbounded_streaming_operations(run, arguments):
    result = run("--json", *arguments)

    assert result.returncode == 2
    assert result.stderr == ""
    body = json.loads(result.stdout)
    assert body["ok"] is False
    assert body["operation"] == arguments[0]
    assert body["error"]["code"] == "invalid_argument"
    assert "unbounded output" in body["error"]["message"]
