import json
from pathlib import Path

import pytest

from scripts.update_hermes_image import (
    IMAGE_NAME,
    UpdateError,
    apply_update,
    plan_update,
)

CURRENT_DIGEST = "sha256:" + "a" * 64
CANDIDATE_DIGEST = "sha256:" + "b" * 64
CURRENT_REVISION = "1" * 40
RELEASE_REVISION = "2" * 40


def image(digest, amd64_revision, arm64_revision=None):
    return {
        "manifest": {"digest": digest},
        "image": {
            "linux/amd64": {
                "config": {"Labels": {"org.opencontainers.image.revision": amd64_revision}}
            },
            "linux/arm64": {
                "config": {
                    "Labels": {
                        "org.opencontainers.image.revision": arm64_revision or amd64_revision
                    }
                }
            },
        },
    }


class FakeRunner:
    def __init__(self, status="ahead", candidate_revision=RELEASE_REVISION, arm_revision=None):
        self.status = status
        self.candidate_revision = candidate_revision
        self.arm_revision = arm_revision

    def json(self, *command):
        joined = " ".join(command)
        if "releases/latest" in joined:
            return {"draft": False, "prerelease": False, "tag_name": "v2026.9.8"}
        if "commits/v2026.9.8" in joined:
            return {"sha": RELEASE_REVISION}
        if "compare/" in joined:
            assert f"{CURRENT_REVISION}...{RELEASE_REVISION}" in joined
            return {"status": self.status}
        if f"{IMAGE_NAME}@{CURRENT_DIGEST}" in command:
            return image(CURRENT_DIGEST, CURRENT_REVISION)
        if f"{IMAGE_NAME}:v2026.9.8" in command:
            return image(CANDIDATE_DIGEST, self.candidate_revision, self.arm_revision)
        raise AssertionError(command)


def repository(tmp_path: Path) -> Path:
    (tmp_path / "runtime").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "runtime/stack.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "images": {"hermes_local": {"reference": f"{IMAGE_NAME}@{CURRENT_DIGEST}"}},
                "artifacts": {},
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    (tmp_path / "tests/test_json_contract.py").write_text(
        f'EXPECTED = "{CURRENT_DIGEST.removeprefix("sha256:")}"\n'
    )
    return tmp_path


def test_newer_release_updates_lock_and_contract_tripwire(tmp_path):
    root = repository(tmp_path)
    plan = plan_update(root, FakeRunner())

    assert plan.changed is True
    apply_update(root, plan)

    stack = json.loads((root / "runtime/stack.json").read_text())
    assert stack["images"]["hermes_local"]["reference"] == (f"{IMAGE_NAME}@{CANDIDATE_DIGEST}")
    assert (
        CANDIDATE_DIGEST.removeprefix("sha256:")
        in (root / "tests/test_json_contract.py").read_text()
    )


def test_release_behind_current_image_is_not_applied(tmp_path):
    plan = plan_update(repository(tmp_path), FakeRunner("behind"))

    assert plan.changed is False
    assert "ahead" in plan.reason


def test_release_rebuild_from_the_same_revision_is_applied(tmp_path):
    plan = plan_update(repository(tmp_path), FakeRunner("identical"))

    assert plan.changed is True
    assert "different image" in plan.reason


def test_release_image_must_match_the_release_commit(tmp_path):
    with pytest.raises(UpdateError, match="different source revisions"):
        plan_update(repository(tmp_path), FakeRunner(candidate_revision="3" * 40))


def test_platform_images_must_share_one_source_revision(tmp_path):
    with pytest.raises(UpdateError, match="do not share"):
        plan_update(repository(tmp_path), FakeRunner(arm_revision="3" * 40))


def test_diverged_history_requires_manual_review(tmp_path):
    with pytest.raises(UpdateError, match="diverged"):
        plan_update(repository(tmp_path), FakeRunner("diverged"))


def test_apply_refuses_a_changed_contract_tripwire_without_partial_write(tmp_path):
    root = repository(tmp_path)
    plan = plan_update(root, FakeRunner())
    (root / "tests/test_json_contract.py").write_text("# digest was moved elsewhere\n")

    with pytest.raises(UpdateError, match="tripwire"):
        apply_update(root, plan)
    assert CURRENT_DIGEST in (root / "runtime/stack.json").read_text()
