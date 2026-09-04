"""agent-mgr cron-sync: the seam between the registry and the in-container converger."""
import os
from pathlib import Path

import pytest

from conftest import fake_docker


def _bin(tmp_path, name, **kw):
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, **kw)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


@pytest.mark.parametrize("descriptor,why", [
    # Unset means the agent declares no shipped jobs -- a refusal, never a
    # silent no-op that reads as 'everything converged'.
    ("", "AGENT_CRON_SPEC"),
    ("AGENT_CRON_SPEC=crons.json\n", "crons.json"),   # named but absent
])
def test_cron_sync_preflight_refusals(run, instance, descriptor, why):
    run("register", "str", str(instance("str", descriptor=descriptor)))
    r = run("cron-sync", "str")
    assert r.returncode != 0
    assert why in r.stderr


def test_refuses_when_the_gateway_is_not_running(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_CRON_SPEC=crons.json\n")
    (repo / "crons.json").write_text(
        '[{"name": "j", "schedule": "0 6 * * *", "prompt": "p", "deliver": "local"}]')
    run("register", "str", str(repo))
    r = run("cron-sync", "str", env=_bin(tmp_path, "str", running=False))
    assert r.returncode != 0
    assert "not running" in r.stderr


@pytest.mark.parametrize(
    "home_env, target",
    [("", "/opt/data"), ("AGENT_BOOT_CONTRACT=plow-init\n", "/var/lib/hermes")],
    ids=["legacy", "plow-init"],
)
def test_pipes_converger_and_spec_into_the_container(run, instance, tmp_path, home_env, target):
    """cron-sync execs (not a start verb), so no credential gate applies --
    nothing to materialize here."""
    repo = instance("str", descriptor="AGENT_CRON_SPEC=crons.json\n")
    (repo / "crons.json").write_text(
        '[{"name": "j", "schedule": "0 6 * * *", "prompt": "p", "deliver": "local"}]')
    run("register", "str", str(repo))
    if home_env:
        home = tmp_path / "home" / ".hermes-str"
        home.mkdir(parents=True)
        (home / ".env").write_text(home_env)
    log = tmp_path / "docker.log"
    r = run("cron-sync", "str", env=_bin(tmp_path, "str", log=log, target=target))
    assert r.returncode == 0, r.stderr
    joined = log.read_text()
    assert "exec -T" in joined and "python3" in joined
    # The converger runs as HOME=<wherever THIS agent's home is actually
    # mounted> -- plow-init's is /var/lib/hermes, not agent-mgr's own
    # /opt/data convention -- or it writes state nothing reads back.
    assert f"HOME={target}" in joined
    # The spec travels as an argument, resolved from the agent's repo...
    assert '"name": "j"' in joined
    # ...and the converger travels over stdin, per invocation -- nothing is
    # installed into the container or the home, so nothing can go stale.
    assert "def load_spec" in Path(f"{log}.stdin").read_text()
