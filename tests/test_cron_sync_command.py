"""agent-mgr cron-sync: the seam between the registry and the in-container converger."""
import os
from pathlib import Path

from conftest import fake_docker


def _bin(tmp_path, name, **kw):
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, **kw)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def test_refuses_without_a_spec(run, instance):
    """Unset means the agent declares no shipped jobs -- a refusal, never a
    silent no-op that reads as 'everything converged'."""
    run("register", "str", str(instance("str")))
    r = run("cron-sync", "str")
    assert r.returncode != 0
    assert "AGENT_CRON_SPEC" in r.stderr


def test_refuses_a_missing_spec_file(run, instance):
    run("register", "str", str(instance("str", descriptor="AGENT_CRON_SPEC=crons.json\n")))
    r = run("cron-sync", "str")
    assert r.returncode != 0
    assert "crons.json" in r.stderr


def test_refuses_when_the_gateway_is_not_running(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_CRON_SPEC=crons.json\n")
    (repo / "crons.json").write_text(
        '[{"name": "j", "schedule": "0 6 * * *", "prompt": "p", "deliver": "local"}]')
    run("register", "str", str(repo))
    r = run("cron-sync", "str", env=_bin(tmp_path, "str", running=False))
    assert r.returncode != 0
    assert "not running" in r.stderr


def test_pipes_converger_and_spec_into_the_container(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_CRON_SPEC=crons.json\n")
    (repo / "crons.json").write_text(
        '[{"name": "j", "schedule": "0 6 * * *", "prompt": "p", "deliver": "local"}]')
    run("register", "str", str(repo))
    log = tmp_path / "docker.log"
    r = run("cron-sync", "str", env=_bin(tmp_path, "str", log=log))
    assert r.returncode == 0, r.stderr
    joined = log.read_text()
    assert "exec -T" in joined and "python3" in joined
    # The spec travels as an argument, resolved from the agent's repo...
    assert '"name": "j"' in joined
    # ...and the converger travels over stdin, per invocation -- nothing is
    # installed into the container or the home, so nothing can go stale.
    assert "def load_spec" in Path(f"{log}.stdin").read_text()
