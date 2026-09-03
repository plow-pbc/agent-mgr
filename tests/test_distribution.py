import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from conftest import fake_curl, install_fake_gh

ROOT = Path(__file__).resolve().parent.parent


def test_release_zipapp_contains_every_resource_needed_for_deploy(tmp_path):
    artifact = tmp_path / "agent-mgr.pyz"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_zipapp.py"), str(artifact)],
        check=True,
    )
    with zipfile.ZipFile(artifact) as archive:
        assert {"LICENSE", "NOTICE"} <= set(archive.namelist())
    binary = fake_curl(tmp_path)
    install_fake_gh(tmp_path, binary)
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "AGENT_MGR_REGISTRY": str(tmp_path / "registry"),
        "PATH": f"{binary}:{os.environ['PATH']}",
    }
    repo = tmp_path / "rowan-hermes-agent"

    created = subprocess.run(
        [str(artifact), "new", "rowan", str(repo)],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    deployed = subprocess.run(
        [str(artifact), "deploy", "rowan"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert deployed.returncode == 0, deployed.stderr
    assert (tmp_path / "home" / ".hermes-rowan" / "config.yaml").is_file()
    assert (
        tmp_path
        / "home"
        / ".hermes-rowan"
        / "plugins"
        / "plow-chat-platform"
        / "plugin.yaml"
    ).is_file()
    resolved = subprocess.run(
        [str(artifact), "--json", "resolve", "rowan"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert resolved.returncode == 0, resolved.stderr
    assert json.loads(resolved.stdout)["result"]["name"] == "rowan"
    cloud = subprocess.run(
        [str(artifact), "--json", "cloud-list"],
        env={"PATH": os.environ["PATH"]},
        text=True,
        capture_output=True,
        check=False,
    )
    assert cloud.returncode == 1
    assert json.loads(cloud.stdout)["error"]["code"] == "configuration_error"
    assert cloud.stderr == ""
