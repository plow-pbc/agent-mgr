import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_stack_lock_has_valid_immutable_artifact_coordinates():
    lock = json.loads((ROOT / "runtime" / "stack.json").read_text())

    assert lock["schema_version"] == 1
    assert lock["images"]["hermes_local"]["reference"].startswith(
        "nousresearch/hermes-agent@sha256:"
    )
    assert set(lock["artifacts"]) == {
        "plow_chat_plugin",
        "plow_chat_activation",
        "google_workspace_skill",
        "plow_invite_skill",
    }
    for artifact in lock["artifacts"].values():
        assert artifact["repository"].count("/") == 1
        assert len(artifact["revision"]) == 40
        assert set(artifact["revision"]) <= set("0123456789abcdef")
        assert artifact["source"]
        assert artifact["destination"]
        assert "scope" not in artifact
