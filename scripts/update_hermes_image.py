#!/usr/bin/env python3
"""Propose a digest-pinned agent-mgr update for the latest Hermes release."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parent.parent
STACK_PATH = Path("runtime/stack.json")
CONTRACT_TEST_PATH = Path("tests/test_json_contract.py")
IMAGE_NAME = "nousresearch/hermes-agent"
HERMES_REPOSITORY = "NousResearch/hermes-agent"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
RELEASE_TAG = re.compile(r"^v\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+)?$")
PLATFORMS = ("linux/amd64", "linux/arm64")


class UpdateError(RuntimeError):
    """A remote or local contract made an automatic update unsafe."""


class Runner(Protocol):
    def json(self, *command: str) -> dict[str, object]: ...


class CommandRunner:
    def json(self, *command: str) -> dict[str, object]:
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
            parsed = json.loads(completed.stdout)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or "").strip() or f"exit {error.returncode}"
            raise UpdateError(f"{command[0]} failed: {detail}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise UpdateError(f"could not read JSON from {command[0]}: {error}") from error
        return _object(parsed, f"{command[0]} response")


def _object(value: object, subject: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise UpdateError(f"{subject} is not an object")
    return value


def _string(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value:
        raise UpdateError(f"{subject} is not a string")
    return value


def _digest(value: object, subject: str) -> str:
    parsed = _string(value, subject)
    if not SHA256.fullmatch(parsed):
        raise UpdateError(f"{subject} is not a sha256 digest")
    return parsed


def _git_sha(value: object, subject: str) -> str:
    parsed = _string(value, subject)
    if not GIT_SHA.fullmatch(parsed):
        raise UpdateError(f"{subject} is not a 40-character commit")
    return parsed


@dataclass(frozen=True, slots=True)
class Release:
    tag: str
    revision: str


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    digest: str
    revision: str


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    changed: bool
    reason: str
    release: Release
    current: ResolvedImage
    candidate: ResolvedImage


def latest_release(runner: Runner) -> Release:
    release = runner.json("gh", "api", f"repos/{HERMES_REPOSITORY}/releases/latest")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateError("GitHub's latest Hermes release is not stable")
    tag = _string(release.get("tag_name"), "Hermes release tag")
    if not RELEASE_TAG.fullmatch(tag):
        raise UpdateError(f"unexpected Hermes release tag: {tag}")
    commit = runner.json("gh", "api", f"repos/{HERMES_REPOSITORY}/commits/{tag}")
    return Release(tag=tag, revision=_git_sha(commit.get("sha"), "Hermes release commit"))


def inspect_image(runner: Runner, reference: str) -> ResolvedImage:
    if SHA256.fullmatch(reference):
        image = f"{IMAGE_NAME}@{reference}"
    elif RELEASE_TAG.fullmatch(reference):
        image = f"{IMAGE_NAME}:{reference}"
    else:
        raise UpdateError(f"invalid Hermes image reference: {reference}")
    inspected = runner.json(
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        image,
        "--format",
        "{{json .}}",
    )
    manifest = _object(inspected.get("manifest"), "Hermes image manifest")
    digest = _digest(manifest.get("digest"), "Hermes image digest")
    if reference.startswith("sha256:") and digest != reference:
        raise UpdateError("Docker returned a different digest for the current image")

    images = _object(inspected.get("image"), "Hermes platform images")
    revisions: set[str] = set()
    for platform in PLATFORMS:
        platform_image = _object(images.get(platform), f"Hermes {platform} image")
        config = _object(platform_image.get("config"), f"Hermes {platform} config")
        labels = _object(config.get("Labels"), f"Hermes {platform} labels")
        revisions.add(
            _git_sha(
                labels.get("org.opencontainers.image.revision"),
                f"Hermes {platform} source revision",
            )
        )
    if len(revisions) != 1:
        raise UpdateError("Hermes platform images do not share one source revision")
    return ResolvedImage(digest=digest, revision=revisions.pop())


def current_digest(root: Path) -> str:
    stack = _object(json.loads((root / STACK_PATH).read_text()), "runtime stack")
    images = _object(stack.get("images"), "runtime stack images")
    hermes = _object(images.get("hermes_local"), "Hermes image lock")
    reference = _string(hermes.get("reference"), "Hermes image reference")
    prefix = f"{IMAGE_NAME}@"
    if not reference.startswith(prefix):
        raise UpdateError(f"Hermes image must use {prefix}<digest>")
    return _digest(reference.removeprefix(prefix), "current Hermes image digest")


def comparison_status(runner: Runner, base: str, head: str) -> str:
    _git_sha(base, "current image revision")
    _git_sha(head, "release image revision")
    comparison = runner.json("gh", "api", f"repos/{HERMES_REPOSITORY}/compare/{base}...{head}")
    status = _string(comparison.get("status"), "GitHub comparison status")
    if status not in {"ahead", "behind", "diverged", "identical"}:
        raise UpdateError(f"unexpected GitHub comparison status: {status}")
    return status


def plan_update(root: Path, runner: Runner) -> UpdatePlan:
    current = inspect_image(runner, current_digest(root))
    release = latest_release(runner)
    candidate = inspect_image(runner, release.tag)
    if candidate.revision != release.revision:
        raise UpdateError("the Hermes release tag and image have different source revisions")
    if candidate.digest == current.digest:
        return UpdatePlan(False, "latest release is already pinned", release, current, candidate)

    comparison = comparison_status(runner, current.revision, release.revision)
    if comparison == "behind":
        return UpdatePlan(
            False,
            "the pinned image is ahead of the latest stable release",
            release,
            current,
            candidate,
        )
    if comparison not in {"ahead", "identical"}:
        raise UpdateError("the pinned image and latest release have diverged; review manually")
    reason = (
        "a newer stable Hermes release is available"
        if comparison == "ahead"
        else "the stable release has a different image for the pinned source revision"
    )
    return UpdatePlan(True, reason, release, current, candidate)


def apply_update(root: Path, plan: UpdatePlan) -> None:
    if not plan.changed:
        return
    stack_path = root / STACK_PATH
    stack = _object(json.loads(stack_path.read_text()), "runtime stack")
    images = _object(stack.get("images"), "runtime stack images")
    hermes = _object(images.get("hermes_local"), "Hermes image lock")
    old_reference = f"{IMAGE_NAME}@{plan.current.digest}"
    if hermes.get("reference") != old_reference:
        raise UpdateError("runtime stack changed after the update was planned")
    hermes["reference"] = f"{IMAGE_NAME}@{plan.candidate.digest}"
    updated_stack = json.dumps(stack, separators=(",", ":")) + "\n"

    contract_path = root / CONTRACT_TEST_PATH
    contract = contract_path.read_text()
    old_hex = plan.current.digest.removeprefix("sha256:")
    new_hex = plan.candidate.digest.removeprefix("sha256:")
    if contract.count(old_hex) != 1 or new_hex in contract:
        raise UpdateError("the JSON contract digest tripwire is not in its expected state")
    stack_path.write_text(updated_stack)
    contract_path.write_text(contract.replace(old_hex, new_hex))


def write_github_output(path: Path, plan: UpdatePlan) -> None:
    values = {
        "changed": str(plan.changed).lower(),
        "release_tag": plan.release.tag,
        "current_digest": plan.current.digest,
        "candidate_digest": plan.candidate.digest,
        "current_revision": plan.current.revision,
        "release_revision": plan.release.revision,
    }
    with path.open("a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="update the lock and its test tripwire"
    )
    parser.add_argument("--github-output", type=Path, help="append metadata for GitHub Actions")
    options = parser.parse_args(arguments)
    try:
        plan = plan_update(ROOT, CommandRunner())
        if options.write:
            apply_update(ROOT, plan)
        if options.github_output:
            write_github_output(options.github_output, plan)
    except (OSError, ValueError, UpdateError) as error:
        print(f"update-hermes-image: {error}", file=sys.stderr)
        return 1
    print(
        f"{plan.reason}: {plan.release.tag} {plan.candidate.digest} "
        f"({plan.candidate.revision[:12]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
