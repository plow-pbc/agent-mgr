import os
import pytest

import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_deploy_installs_the_config_into_the_agents_home(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("deploy", "rowan")
    assert r.returncode == 0, r.stderr
    installed = tmp_path / "home" / ".hermes-rowan" / "config.yaml"
    assert installed.exists()
    assert "openai-codex" in installed.read_text()


def test_deploy_writes_a_dotenv_skeleton_carrying_both_platforms(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    env = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "PLOW_AGENT_TOKEN" in env
    assert "PLOW_HOME_CHANNEL" in env
    assert "DOMO_MCP_TOKEN" in env, "latch is baseline, not an opt-in"


def test_deploy_never_clobbers_an_existing_dotenv(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    env.write_text("PLOW_AGENT_TOKEN=real\n")
    run("deploy", "rowan")
    assert env.read_text() == "PLOW_AGENT_TOKEN=real\n"


def test_migrate_plugin_env_copies_legacy_names_and_is_idempotent(run, instance, tmp_path):
    """The fleet migration step: legacy PLOW_CHAT_* values land under the names
    the unified plugin reads, the old lines stay (a pre-rename plugin still
    reads them mid-migration; a later cleanup removes them), and a second run
    writes nothing."""
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    env.write_text("PLOW_CHAT_TOKEN=tok_plow\nPLOW_CHAT_CHAT_UID=cht_dm\nHOSTEX_TOKEN=keepme\n")

    r = run("migrate-plugin-env", "rowan")
    assert r.returncode == 0, r.stderr
    lines = env.read_text().splitlines()
    assert "PLOW_AGENT_TOKEN=tok_plow" in lines
    assert "PLOW_HOME_CHANNEL=cht_dm" in lines
    assert "PLOW_CHAT_TOKEN=tok_plow" in lines, "the legacy lines must survive until the cleanup"
    assert "HOSTEX_TOKEN=keepme" in lines
    # One ledger line per var written, no values on stdout.
    assert "wrote PLOW_AGENT_TOKEN from PLOW_CHAT_TOKEN" in r.stdout
    assert "wrote PLOW_HOME_CHANNEL from PLOW_CHAT_CHAT_UID" in r.stdout
    assert "tok_plow" not in r.stdout + r.stderr, "a credential value leaked into the ledger"

    before = env.read_text()
    r = run("migrate-plugin-env", "rowan")
    assert r.returncode == 0, r.stderr
    assert env.read_text() == before, "a second run must write nothing"
    assert "wrote" not in r.stdout


def test_a_redeploy_migrates_a_legacy_only_dotenv(run, instance, tmp_path):
    """The public path migrates, not just the manual rollout order: a
    legacy-only agent redeployed onto the unified plugin must come back with the
    names it reads, or it silently loses its phone line."""
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    env.write_text("PLOW_CHAT_TOKEN=tok_plow\nPLOW_CHAT_CHAT_UID=cht_dm\n")

    r = run("deploy", "rowan")
    assert r.returncode == 0, r.stderr
    lines = env.read_text().splitlines()
    assert "PLOW_AGENT_TOKEN=tok_plow" in lines
    assert "PLOW_HOME_CHANNEL=cht_dm" in lines


def test_migration_resolves_a_duplicated_key_like_its_readers(run, instance, tmp_path):
    """Last declaration wins -- dotenv_read and the compose env_file loader
    both resolve a duplicated key to its last line, so the migrated value must
    be the one the gateway actually ran with."""
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    env.write_text("PLOW_CHAT_TOKEN=tok_stale\nPLOW_CHAT_TOKEN=tok_live\n")

    r = run("migrate-plugin-env", "rowan")
    assert r.returncode == 0, r.stderr
    assert "PLOW_AGENT_TOKEN=tok_live" in env.read_text().splitlines()


def test_migrate_plugin_env_sync_overwrites_for_recovery(run, instance, tmp_path):
    """The recovery command activate prints must be able to finish the job.
    Idempotent mode skips set keys, so after a failed in-activate sync the
    fresh token sits only under the legacy name — `--sync` is the forwarded
    mode that overwrites."""
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    env.write_text("PLOW_CHAT_TOKEN=tok_fresh\nPLOW_AGENT_TOKEN=tok_stale\n")
    r = run("migrate-plugin-env", "rowan", "--sync")
    assert r.returncode == 0, r.stderr
    lines = env.read_text().splitlines()
    assert "PLOW_AGENT_TOKEN=tok_fresh" in lines
    assert "PLOW_AGENT_TOKEN=tok_stale" not in lines


def test_migrate_plugin_env_rejects_an_unknown_mode(run, instance, tmp_path):
    """Fail-fast on a typo'd flag: silently running in the OTHER mode is the
    stale-token bug this pair of modes exists to prevent."""
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    r = run("migrate-plugin-env", "rowan", "--bogus")
    assert r.returncode != 0
    assert "unknown mode" in r.stderr and "--sync" in r.stderr


def test_migrate_plugin_env_without_a_dotenv_points_at_deploy(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("migrate-plugin-env", "rowan")
    assert r.returncode != 0
    assert "deploy" in r.stderr


def test_installed_state_is_not_reachable_by_other_users(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    for f in ("config.yaml", ".env"):
        mode = (tmp_path / "home" / ".hermes-rowan" / f).stat().st_mode
        assert not (mode & stat.S_IRWXO), f"{f} is reachable by other users"


def test_deploy_on_an_instance_with_no_config_is_refused(run, instance):
    run("register", "bare", str(instance("bare", config=None)))
    r = run("deploy", "bare")
    assert r.returncode != 0
    assert "config.yaml" in r.stderr


def test_every_shipped_pin_is_a_sha_not_a_branch():
    """A branch would silently re-point a running agent on the next upstream push.

    Both, because the activate pin gates the one command that is a one-time
    irreversible spend -- a branch name or a truncated SHA in that file would
    otherwise surface only when an operator ran it.
    """
    import json

    artifacts = json.loads((ROOT / "runtime" / "stack.json").read_text())["artifacts"]
    for artifact in artifacts.values():
        ref = artifact["revision"]
        assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)


def test_the_image_pin_is_a_digest_not_a_tag():
    import json

    ref = json.loads((ROOT / "runtime" / "stack.json").read_text())["images"]["hermes_local"][
        "reference"
    ]
    digest = ref.rpartition("@")[2]
    assert digest.startswith("sha256:") and len(digest) == 71


def test_the_shipped_config_template_wires_both_platforms():
    cfg = (ROOT / "templates" / "config.yaml").read_text()
    assert "plow-chat-platform" in cfg
    assert "latch:" in cfg and "DOMO_DEVICE_UID" in cfg


def test_no_template_carries_a_literal_credential():
    for name in ("config.yaml", "env.example", "agent.env"):
        text = (ROOT / "templates" / name).read_text()
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for key in ("PLOW_AGENT_TOKEN", "DOMO_MCP_TOKEN"):
                if line.strip().startswith(f"{key}="):
                    assert line.strip() == f"{key}=", f"{name} ships a value for {key}"


def test_an_agent_can_say_where_its_config_lives(run, instance, tmp_path):
    """The rentals agent keeps config.yaml under runtime/, beside the vault seed
    and SOUL it ships with. Without this it kept a second installer that
    hardcoded both the path and the home -- two owners of the thing agent-mgr
    exists to own."""
    repo = instance("str", descriptor="AGENT_CONFIG=runtime/config.yaml\n", config=None)
    (repo / "runtime").mkdir()
    (repo / "runtime" / "config.yaml").write_text("model:\n  provider: openai-codex\n")
    run("register", "str", str(repo))
    r = run("deploy", "str")
    assert r.returncode == 0, r.stderr
    assert "openai-codex" in (tmp_path / "home" / ".hermes-str" / "config.yaml").read_text()


def test_a_relative_config_path_resolves_against_the_instance_repo(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_CONFIG=runtime/config.yaml\n", config=None)
    (repo / "runtime").mkdir()
    (repo / "runtime" / "config.yaml").write_text("model:\n  provider: x\n")
    run("register", "str", str(repo))
    assert f"AGENT_CONFIG={repo}/runtime/config.yaml" in run("resolve", "str").stdout


def test_a_missing_config_names_the_path_it_looked_at(run, instance):
    """The old message named a directory, which is useless when the whole point
    is that the file is somewhere else."""
    run(
        "register",
        "str",
        str(instance("str", descriptor="AGENT_CONFIG=runtime/config.yaml\n", config=None)),
    )
    r = run("deploy", "str")
    assert r.returncode != 0
    assert "runtime/config.yaml" in r.stderr
    assert "AGENT_CONFIG" in r.stderr


def test_an_instance_dotenv_example_wins_over_the_fleet_template(run, instance, tmp_path):
    """An agent with extra credentials knows its dotenv contract better than the
    fleet template does; a skeleton missing those keys is a first run that looks
    complete and is not."""
    repo = instance("str")
    (repo / ".env.example").write_text("HOSTEX_TOKEN=\nSEAM_API_KEY=\nPLOW_AGENT_TOKEN=\n")
    run("register", "str", str(repo))
    run("deploy", "str")
    env = (tmp_path / "home" / ".hermes-str" / ".env").read_text()
    assert "HOSTEX_TOKEN" in env and "SEAM_API_KEY" in env


def test_the_fleet_template_is_used_when_an_instance_ships_none(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("deploy", "rowan")
    env = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "PLOW_AGENT_TOKEN" in env and "DOMO_MCP_TOKEN" in env


def test_deploy_is_the_whole_deploy_including_the_instances_own_step(run, instance, tmp_path):
    """One command, one owner. The alternative -- agent-mgr doing its half and
    the README telling the operator to run the rest in order -- moves ownership
    to whoever reads the docs, which is not an owner at all."""
    repo = instance("str", descriptor="AGENT_DEPLOY_HOOK=scripts/seed.sh\n")
    (repo / "scripts").mkdir()
    hook = repo / "scripts" / "seed.sh"
    hook.write_text(f"#!/usr/bin/env bash\ntouch {tmp_path / 'hook-ran'}\n")
    hook.chmod(0o755)
    run("register", "str", str(repo))
    r = run("deploy", "str")
    assert r.returncode == 0, r.stderr
    home = tmp_path / "home" / ".hermes-str"
    assert (home / "config.yaml").exists(), "config"
    assert (home / ".env").exists(), "dotenv skeleton"
    assert (tmp_path / "hook-ran").exists(), "the instance's own deploy step never ran"


def test_a_failing_hook_fails_the_deploy(run, instance, tmp_path):
    """A hook refuses for a reason -- a missing corpus, a failed composition.
    Swallowing it leaves the caller believing the deploy landed."""
    repo = instance("str", descriptor="AGENT_DEPLOY_HOOK=scripts/seed.sh\n")
    (repo / "scripts").mkdir()
    hook = repo / "scripts" / "seed.sh"
    hook.write_text('#!/usr/bin/env bash\necho "no vault" >&2\nexit 1\n')
    hook.chmod(0o755)
    run("register", "str", str(repo))
    r = run("deploy", "str")
    assert r.returncode != 0
    assert "ARE installed" in r.stderr and "is NOT" in r.stderr


def test_a_declared_hook_that_is_missing_is_named(run, instance):
    run("register", "str", str(instance("str", descriptor="AGENT_DEPLOY_HOOK=scripts/gone.sh\n")))
    r = run("deploy", "str")
    assert r.returncode != 0
    assert "deploy hook" in r.stderr and "gone.sh" in r.stderr


def test_an_agent_with_no_hook_deploys_fine(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    assert run("deploy", "rowan").returncode == 0
    assert (tmp_path / "home" / ".hermes-rowan" / "config.yaml").exists()


@pytest.mark.parametrize(
    ("home_env", "retired"),
    [("/var/lib/hermes", True), ("/opt/data", False)],
    ids=["a-current-base-bundles-them", "a-legacy-base-still-reads-them"],
)
def test_deploy_retires_what_older_deploys_staged_so_the_image_copy_runs(
        run, instance, tmp_path, home_env, retired):
    """A home plugin named plow-chat-platform wins over the image's bundled
    one, and a home skill directory is "yours was kept" to the runtime's
    reconcile -- so every copy an older deploy staged shadows the copy the
    pinned base bundles, and a fix merged upstream never runs (#156). Seeded
    the way the live homes were: plugin, both fleet skills, a fetch-tree
    rollback twin, and the runtime's manifest naming both skills. The manifest
    line has to go too: the reconcile reads an entry with no directory as
    deleted-by-the-user and never re-seeds it.

    Only for an image that bundles what it retires: a legacy base's plugin
    predates the fleet pin, so its home copies are still the ones that work,
    and a deploy onto it must leave them alone."""
    from conftest import fake_docker

    run("register", "rowan", str(instance("rowan")))
    home = tmp_path / "home" / ".hermes-rowan"
    env = {"PATH": f"{fake_docker(tmp_path, home=home, name='rowan', home_env=home_env)}:{os.environ['PATH']}"}
    # A current-contract reload materialises the durable credential first.
    (tmp_path / "home" / ".plow-credentials-rowan").write_text(
        "PLOW_API_BASE=https://api.example\nPLOW_AGENT_TOKEN=tok_x\n"
    )
    staged = [
        home / "plugins" / "plow-chat-platform",
        home / "plugins" / "plow-chat-platform.previous",
        home / "skills" / "productivity" / "google-workspace",
        home / "skills" / "growth" / "plow-invite",
    ]
    for tree in staged:
        tree.mkdir(parents=True)
        (tree / "marker").write_text("staged by an older deploy\n")
    bystander = home / "skills" / "productivity" / "notion"
    bystander.mkdir()
    (bystander / "SKILL.md").write_text("name: notion\n")
    manifest = home / "skills" / ".bundled_manifest"
    before = "notion:aaaa\ngoogle-workspace:bbbb\nplow-invite:cccc\n"
    manifest.write_text(before)

    r = run("deploy", "rowan", env=env)
    assert r.returncode == 0, r.stderr
    if retired:
        assert not any(tree.exists() for tree in staged), "a staged copy survived to shadow the image"
        assert manifest.read_text() == "notion:aaaa\n"
    else:
        assert all(tree.is_dir() for tree in staged), "a legacy base lost the copies it runs"
        assert manifest.read_text() == before
    assert (bystander / "SKILL.md").is_file(), "the operator's own skill was touched"


def test_the_retirement_will_not_read_or_write_a_manifest_through_a_symlink(
        run, instance, tmp_path):
    """A compromised gateway can point skills/.bundled_manifest at any file
    the operator can read. Following it would copy that file's lines back
    into the mounted home as a regular file -- a secret, readable by the
    gateway on its next boot. Refuse, and touch nothing."""
    from conftest import fake_docker

    run("register", "rowan", str(instance("rowan")))
    home = tmp_path / "home" / ".hermes-rowan"
    (home / "skills" / "growth" / "plow-invite").mkdir(parents=True)
    secret = tmp_path / "home" / "operator-secret"
    secret.write_text("SECRET=hunter2\n")
    (home / "skills" / ".bundled_manifest").symlink_to(secret)
    env = {"PATH": f"{fake_docker(tmp_path, home=home, name='rowan', home_env='/var/lib/hermes')}:{os.environ['PATH']}"}

    r = run("deploy", "rowan", env=env)
    assert r.returncode != 0, "the retirement followed a symlinked manifest"
    assert "outside" in r.stderr, r.stderr
    assert (home / "skills" / ".bundled_manifest").is_symlink(), "the symlink was replaced"
    assert secret.read_text() == "SECRET=hunter2\n"
    assert (home / "skills" / "growth" / "plow-invite").is_dir(), "retired before refusing"


def _transition_env(tmp_path, log=None):
    from conftest import fake_docker

    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan", log=log)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _guarded(instance, run, tmp_path, *, refuses):
    """An instance whose pre-transition guard allows or refuses."""
    repo = instance("rowan", descriptor="AGENT_PRE_TRANSITION=scripts/guard.sh\n")
    (repo / "scripts").mkdir(exist_ok=True)
    g = repo / "scripts" / "guard.sh"
    g.write_text(
        "#!/usr/bin/env bash\n"
        + (
            f'echo "a nightly is mid-ingest" >&2\nexit 1\n'
            if refuses
            else f"touch {tmp_path / 'guard-ran'}\n"
        )
    )
    g.chmod(0o755)
    run("register", "rowan", str(repo))
    return repo


def test_a_refusing_guard_stops_every_transition(run, instance, tmp_path):
    """The rentals agent's guard is a nightly-ingest check. Its doc copies
    drifted across three review rounds; a hook the tool calls has no copies."""
    import os

    _guarded(instance, run, tmp_path, refuses=True)
    from conftest import fake_docker

    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    env = {"PATH": f"{b}:{os.environ['PATH']}"}
    for cmd in (
        ["up", "rowan"],
        ["down", "rowan"],
        ["restart", "rowan"],
        ["compose", "rowan", "up", "-d", "--force-recreate"],
    ):
        r = run(*cmd, env=env)
        assert r.returncode != 0, f"{cmd} transitioned past a refusing guard"
        assert "refused" in r.stderr


def _live(instance, run, tmp_path):
    """A registered agent that declares itself live: real people's workflows
    run through it, so a transition needs a deliberate operator."""
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_LIVE=1\n")))
    from conftest import fake_docker

    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _run_tty(argv, reply, registry, tmp_path, env_path, timeout=None):
    """agent-mgr on a real pty: [ -t 0 ] is the branch these tests exercise."""
    import pty
    import subprocess

    env = dict(os.environ)
    env.update({"AGENT_MGR_REGISTRY": str(registry), "HOME": str(tmp_path / "home"), **env_path})
    master, slave = pty.openpty()
    try:
        os.write(master, f"{reply}\n".encode())
        return subprocess.run(
            [str(ROOT / "agent-mgr"), *argv],
            stdin=slave,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    finally:
        os.close(master)
        os.close(slave)


def test_a_live_agent_refuses_a_non_interactive_transition(run, instance, tmp_path):
    """The gateway messages its person at every restart, so a transition on a
    live agent needs a deliberate operator. Without a terminal
    and without the acknowledgement, every transition route refuses -- and the
    refusal names the acknowledgement, because a deploy script hitting this is
    being told how to say "the restart is the point"."""
    env = _live(instance, run, tmp_path)
    for cmd in (
        ["up", "rowan"],
        ["down", "rowan"],
        ["restart", "rowan"],
        ["compose", "rowan", "up", "-d", "--force-recreate"],
    ):
        r = run(*cmd, env=env)
        assert r.returncode != 0, f"{cmd} transitioned a live agent silently"
        assert "AGENT_TRANSITION_ACK" in r.stderr
    r = run("logs", "rowan", env=env)
    assert "AGENT_TRANSITION_ACK" not in r.stderr, "logs is a read, not a transition"


def test_one_interactive_yes_answers_deploy_and_its_reload(run, registry, instance, tmp_path):
    """deploy asks at its preflight and ends with a reload in a child process.
    The yes is exported, so the child never asks again -- with only ONE answer
    on the pty, a re-prompt would block on the empty terminal and fail this
    test by timeout, and a refusal would fail it by exit code."""
    r = _run_tty(
        ["deploy", "rowan"], "y", registry, tmp_path, _live(instance, run, tmp_path), timeout=120
    )
    assert r.returncode == 0, r.stderr
    # And the reload actually ran -- exit 0 with the reload silently skipped
    # would leave this test covering nothing.
    assert "restarting rowan's gateway" in r.stdout, r.stdout


def test_restart_and_deploy_reload_recreate_the_container(run, instance, tmp_path):
    """A Compose template change reaches existing agents only on recreation."""
    from conftest import fake_docker

    run("register", "rowan", str(instance("rowan")))
    log = tmp_path / "docker-argv"
    b = fake_docker(
        tmp_path,
        home=tmp_path / "home" / ".hermes-rowan",
        name="rowan",
        log=log,
    )
    env = {"PATH": f"{b}:{os.environ['PATH']}"}

    for command in (("restart", "rowan"), ("deploy", "rowan")):
        log.write_text("")
        r = run(*command, env=env)
        assert r.returncode == 0, r.stderr
        assert any(
            line.endswith("up -d --force-recreate hermes") for line in log.read_text().splitlines()
        ), f"{command[0]} did not recreate the container:\n{log.read_text()}"


def test_an_unacknowledged_deploy_refuses_before_it_writes(run, instance, tmp_path):
    """deploy's preflight rule: a command that installs everything before
    refusing has already done the thing the refusal exists to prevent. The
    ack check sits in the preflight beside the veto, so the home stays
    untouched -- and the same deploy proceeds once acknowledged."""
    env = _live(instance, run, tmp_path)
    r = run("deploy", "rowan", env=env)
    assert r.returncode != 0
    assert "AGENT_TRANSITION_ACK" in r.stderr
    assert not (tmp_path / "home" / ".hermes-rowan" / "config.yaml").exists(), (
        "deploy wrote into the home before refusing"
    )
    r = run("deploy", "rowan", env={**env, "AGENT_TRANSITION_ACK": "1"})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "home" / ".hermes-rowan" / "config.yaml").is_file()


@pytest.mark.parametrize("reply,ok", [("y", True), ("yes", True), ("n", False), ("", False)])
def test_the_interactive_prompt_defaults_to_no(run, registry, instance, tmp_path, reply, ok):
    """A real pty, because [ -t 0 ] is the branch under test. Only an explicit
    yes proceeds; empty and garbage refuse -- the default answer to "message a
    real person?" is No."""
    r = _run_tty(["up", "rowan"], reply, registry, tmp_path, _live(instance, run, tmp_path))
    assert (r.returncode == 0) == ok, (reply, r.stderr)


def test_activate_reports_success_when_the_guard_refuses_its_reload(run, instance, tmp_path):
    """The one command a refusal must not fail. By the reload the one-time
    activation is already spent and the token written, so a red exit reads as
    "activation failed" -- and the natural response is to run it again, spending
    a second activation to recover from a guard that said "not right now"."""
    import os

    _guarded(instance, run, tmp_path, refuses=True)
    from conftest import fake_docker

    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    (tmp_path / "home" / ".hermes-rowan").mkdir(parents=True, exist_ok=True)

    r = run("activate", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, f"a refused reload failed an activation that had landed: {r.stderr}"
    assert "do NOT re-run activate" in r.stderr, (
        "the operator was not told the activation succeeded, which is the whole point"
    )


@pytest.mark.parametrize(
    "args",
    [
        ("sign-in", "rowan"),
        ("add-skill", "rowan", "plow-pbc/property-hunt", "--ref", "a" * 40),
    ],
)
def test_every_other_write_then_reload_still_fails_on_a_refused_guard(
    run, instance, tmp_path, args
):
    """The negative half of `activate` being "the one command a refusal does not
    fail". These are in the same position -- the write has landed by the
    reload -- so activate's `|| echo ...SUCCEEDED...` is the obvious next
    copy-paste, and it would make the word "one" false with a green suite."""
    import os

    _guarded(instance, run, tmp_path, refuses=True)
    from conftest import fake_docker, fake_skill_gh

    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True, exist_ok=True)
    # What each subcommand needs BEFORE its reload, so the refusal is what stops
    # it rather than a missing precondition: sign-in reads the installed config,
    # add-skill fetches a tarball. A RUNNING gateway for both -- the reload
    # exits before the guard when there is none.
    (home / "config.yaml").write_text("model:\n  provider: openai-codex\n")
    # A real home always has a dotenv (deploy writes the skeleton first).
    (home / ".env").write_text("")
    b = fake_skill_gh(tmp_path)
    fake_docker(tmp_path, home=home, name="rowan")
    r = run(*args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, f"{args[0]} reported success past a refusing guard"
    assert "refused" in r.stderr, f"{args[0]} did not name the refusal: {r.stderr}"


def test_the_guard_runs_before_a_transition_and_not_before_a_read(run, instance, tmp_path):
    import os

    _guarded(instance, run, tmp_path, refuses=False)
    from conftest import fake_docker

    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    env = {"PATH": f"{b}:{os.environ['PATH']}"}
    marker = tmp_path / "guard-ran"

    run("logs", "rowan", env=env)
    assert not marker.exists(), "a read ran the guard"

    run("up", "rowan", env=env)
    assert marker.exists(), "a transition did not run the guard"


def test_a_declared_guard_that_is_missing_is_named(run, instance, tmp_path):
    import os

    run(
        "register",
        "rowan",
        str(instance("rowan", descriptor="AGENT_PRE_TRANSITION=scripts/gone.sh\n")),
    )
    from conftest import fake_docker

    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    r = run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "pre-transition guard" in r.stderr and "gone.sh" in r.stderr


def test_an_agent_with_no_guard_transitions_freely(run, instance, tmp_path):
    import os
    from conftest import fake_docker

    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    assert run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"}).returncode == 0


@pytest.mark.parametrize(
    "args",
    [
        ["up", "rowan"],
        ["down", "rowan"],
        ["restart", "rowan"],
        ["compose", "rowan", "up", "-d", "--force-recreate"],
        ["compose", "rowan", "start"],
        ["compose", "rowan", "pause"],
        ["compose", "rowan", "unpause"],
        ["compose", "rowan", "stop"],
    ],
)
def test_no_route_to_a_transition_bypasses_the_veto(run, instance, tmp_path, args):
    """Every route goes through compose_transition, so a new call site cannot
    forget the veto. start/pause/unpause interrupt a running process as surely
    as down does, and the earlier list omitted all three."""
    _guarded(instance, run, tmp_path, refuses=True)
    r = run(*args, env=_transition_env(tmp_path))
    assert r.returncode != 0, f"{args} transitioned past a refusing guard"
    assert "refused" in r.stderr


def test_a_reload_is_a_transition_too(run, instance, tmp_path):
    """deploy writes and then reloads. Routing the reload around the veto let
    four write-then-reload subcommands restart the container mid-ingest."""
    repo = _guarded(instance, run, tmp_path, refuses=False)
    # Allow the deploy's own pre-write veto, then refuse by the time it reloads.
    (repo / "scripts" / "guard.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"n=$(cat {tmp_path}/count 2>/dev/null || echo 0); echo $((n+1)) > {tmp_path}/count\n"
        '[ "$n" = 0 ] || { echo "a nightly started" >&2; exit 1; }\n'
    )
    (repo / "scripts" / "guard.sh").chmod(0o755)
    r = run("deploy", "rowan", env=_transition_env(tmp_path))
    assert r.returncode != 0
    assert "refused" in r.stderr


def test_the_subcommand_is_classified_not_the_flattened_argv(run, instance, tmp_path):
    """`up` can appear in a prompt, a filename or a flag value. Matching the
    flattened "$*" made those look like transitions."""
    _guarded(instance, run, tmp_path, refuses=True)
    r = run(
        "compose",
        "rowan",
        "exec",
        "hermes",
        "echo",
        "please up the volume",
        env=_transition_env(tmp_path),
    )
    assert r.returncode == 0, r.stderr


def _pinned_skill_agent(run, instance, tmp_path):
    """rowan with one skills.tsv pin, a `gh` serving that skill's tarball so
    the REAL fetch-tree runs, and conftest's docker -- which answers `config`,
    where the bare stub made resolve-guard refuse at the reload after the
    skill had installed. Returns the home and the PATH env to deploy with."""
    from conftest import fake_docker, fake_skill_gh

    repo = instance("rowan")
    (repo / "skills.tsv").write_text(f"plow-pbc/x\t{'a' * 40}\tmy-skill\t\n")
    run("register", "rowan", str(repo))
    home = tmp_path / "home" / ".hermes-rowan"
    gh = fake_skill_gh(tmp_path, skill_name="my-skill")
    docker = fake_docker(tmp_path, home=home, name="rowan")
    return home, {"PATH": f"{gh}:{docker}:{os.environ['PATH']}"}


def test_deploy_replays_every_pinned_skill(run, instance, tmp_path):
    """It is advertised as the whole deploy. A rebuild that omitted them left an
    agent whose skills.tsv said one thing and whose home held another."""
    home, env = _pinned_skill_agent(run, instance, tmp_path)
    r = run("deploy", "rowan", env=env)
    assert r.returncode == 0, r.stderr
    assert (home / "skills" / "my-skill" / "SKILL.md").exists()


def test_deploy_replaces_a_container_planted_config_symlink(run, instance, tmp_path):
    repo = instance("rowan", config="model:\n  provider: openai-codex\n")
    run("register", "rowan", str(repo))
    run("deploy", "rowan")
    target = tmp_path / "sibling.env"
    target.write_text("PLOW_AGENT_TOKEN=keep\n")
    config = tmp_path / "home" / ".hermes-rowan" / "config.yaml"
    config.unlink()
    config.symlink_to(target)

    r = run("deploy", "rowan")

    assert r.returncode == 0, r.stderr
    assert target.read_text() == "PLOW_AGENT_TOKEN=keep\n"
    assert not config.is_symlink()


def test_a_missing_hook_is_caught_before_anything_is_written(run, instance, tmp_path):
    """Validated at the end, a missing hook left the plugin and config installed
    under a message saying the deploy did not land -- a report the state
    contradicts."""
    run(
        "register",
        "rowan",
        str(instance("rowan", descriptor="AGENT_DEPLOY_HOOK=scripts/gone.sh\n")),
    )
    r = run("deploy", "rowan")
    assert r.returncode != 0
    assert "nothing was installed" in r.stderr
    assert not (tmp_path / "home" / ".hermes-rowan" / "config.yaml").exists()


def test_a_runtime_hook_failure_says_what_landed(run, instance, tmp_path):
    repo = _guarded(instance, run, tmp_path, refuses=False)
    (repo / "agent.env").write_text("AGENT_DEPLOY_HOOK=scripts/hook.sh\n")
    h = repo / "scripts" / "hook.sh"
    h.write_text('#!/usr/bin/env bash\necho "no vault" >&2\nexit 1\n')
    h.chmod(0o755)
    r = run("deploy", "rowan", env=_transition_env(tmp_path))
    assert r.returncode != 0
    assert "ARE installed" in r.stderr and "is NOT" in r.stderr


def _block(text, start, end):
    """The lines of one command's block, so a match cannot come from elsewhere.

    The end delimiter is an EXACT line match. A substring match on "}" would hit
    the `{40}` in the SHA regex, and the first version of this used "\n}" --
    which can never match, because the text was already split on newlines. That
    made the plugin "block" run to end-of-file: the positive assertions would
    have stayed green with the ref read moved into any later helper, and the
    negative one spanned ~65 unrelated lines.
    """
    lines = text.split("\n")
    i = next(n for n, l in enumerate(lines) if start in l)
    j = next(n for n, l in enumerate(lines[i + 1 :], i + 1) if l.rstrip() == end)
    return "\n".join(lines[i:j])


def test_the_image_is_the_only_owner_of_the_plugin_and_seed_skills():
    """What an older deploy staged into every home is what the pinned base
    bundles, and a home copy shadows the image's (#156). The activation script
    is the one thing still fetched from hermes-plow-chat, at a pre-strip SHA.
    """
    import json

    artifacts = json.loads((ROOT / "runtime" / "stack.json").read_text())["artifacts"]
    assert set(artifacts) == {"plow_chat_activation"}
    assert artifacts["plow_chat_activation"]["source"] == "ref/scripts/create_plow_chat_curl.sh"


def test_the_activate_pin_is_frozen():
    """The activate ref may not be bumped at all, and this is what enforces it.

    Proving the ref is an ANCESTOR of the strip commit would need that repo's
    history, which is a network call this suite will not make. Pinning the SHA
    needs nothing, and reddens on every forward bump -- so the why lives in the
    failure message below, where whoever tripped it is already looking, rather
    than in a doc they would have to be sent to. The README's builds-on section
    is the same rule for someone reading before they bump.
    """
    import json

    artifacts = json.loads((ROOT / "runtime" / "stack.json").read_text())["artifacts"]
    activate = artifacts["plow_chat_activation"]["revision"]
    assert activate == "98ddb2e7f0ce563a7ed6c9af43802d15b5ff62d3", (
        "the activate pin moved. It is frozen behind `Strip the SEED ceremony`, "
        "which deleted the ref/scripts/ path it names -- a later SHA 404s on "
        "activate. If this is deliberate, the new SHA must still predate that "
        "commit, and the README's builds-on section says why."
    )


def test_an_orphaned_tree_from_a_killed_run_does_not_survive_the_next_install(
    run, instance, tmp_path
):
    """A killed run leaves a valid second skill tree where the gateway looks.

    The trap does not fire on SIGKILL, an OOM kill or a power loss, so the
    staging and backup directories can outlive their run. `.previous` is the
    sharp one: it is a COMPLETE tree carrying `name: my-skill`, beside the
    real one, in the directory the gateway enumerates.

    Both names used to be pid-suffixed, which meant a run only ever cleaned up
    after its own pid-twin -- every other orphan stayed forever. And the
    backup's own rm sits inside the `is there a current install` branch, so on a
    first install that branch is skipped and the orphan survives untouched.
    Seeded here with NO current install, which is the case that got missed.
    """
    home, env = _pinned_skill_agent(run, instance, tmp_path)
    skills = home / "skills"
    orphan = skills / "my-skill.previous"
    orphan.mkdir(parents=True)
    (orphan / "SKILL.md").write_text("name: my-skill\n")
    (skills / "my-skill.incoming").mkdir()

    r = run("deploy", "rowan", env=env)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in skills.iterdir()) == ["my-skill"], (
        "an orphaned tree survived the install"
    )


def test_a_rollback_copy_is_promoted_before_the_next_publication(run, instance, tmp_path):
    """A killed prior publication is recovered before the next atomic swap."""
    home, env = _pinned_skill_agent(run, instance, tmp_path)
    skills = home / "skills"
    rollback = skills / "my-skill.previous"
    rollback.mkdir(parents=True)
    (rollback / "SKILL.md").write_text("name: my-skill\n")

    r = run("deploy", "rowan", env=env)
    assert r.returncode == 0, r.stderr
    assert (skills / "my-skill" / "SKILL.md").is_file(), (
        "the recovered tree was lost during publication"
    )
    assert not rollback.exists()


def test_no_host_side_script_depends_on_a_gnu_only_tool():
    """No host-side script may depend on a tool absent from the macOS floor.

    This has broken twice -- #19's `realpath -m`, then a `flock` added and cut
    within this PR -- and a Linux-only suite cannot see it. Two entries only:
    both are command names, where a word boundary is a complete match. Anything
    needing spelling enumeration belongs to #26, which runs the suite where the
    constraint is real; `readlink -f` is excluded because it is what SETS the
    12.3 floor and the entrypoint depends on it.
    """
    import re

    banned = {
        # Bitten: #19 (realpath) and this PR (flock -- Homebrew-only on the Mac).
        "flock": r"\bflock\b",
        # Boundary, not a flag: BSD realpath ERRORS on a path that does not exist
        # yet, which a first deploy needs, so bare `realpath "$p"` IS the #19
        # break. The lookbehind is what skips os.path.realpath( in a python3 -c.
        "realpath": r"(?<![\w.])realpath\b",
    }
    scripts = [ROOT / "agent-mgr"] + sorted((ROOT / "lib").iterdir())
    for script in scripts:
        # Full-line comments only. common.sh explains why realpath is NOT used,
        # and that sentence must not trip the check it is documenting.
        code = "\n".join(
            l for l in script.read_text().splitlines() if not l.lstrip().startswith("#")
        )
        for tool, pattern in banned.items():
            assert not re.search(pattern, code), (
                f"{script.name} uses {tool}, which is not portable to the "
                "macOS 12.3 floor README commits to -- the suite runs on Linux, "
                "so it lands green here and fails on the operator's Mac"
            )


def test_the_possibly_empty_array_is_always_expansion_guarded():
    """`${AGENT_HOOK_ENV[@]}` must never be expanded without its `+` guard.

    The array is empty for an agent with no extra descriptor keys, and bash
    before 4.4 -- which macOS ships -- treats that as unset under `set -u`, so
    the bare expansion kills `deploy`. common.sh owns why; this pins that the
    guard survives, since it reads like removable ceremony.

    Subscript (`[@]`/`[*]`) and guard (`+`/`:+`) are normalised and `${#...}`
    references dropped, so no spelling of the expansion escapes; the assertion
    is a per-line balance rather than a per-occurrence proof. #26 owns the rest.
    """
    import re

    root_files = [ROOT / "agent-mgr"] + sorted((ROOT / "lib").iterdir())
    for script in root_files:
        for n, line in enumerate(script.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            # `[*]` fails identically to `[@]` on an empty array under
            # `set -u`, so it is normalised BEFORE the filter -- skipping it
            # there was a real false negative, not a cosmetic one.
            line = line.replace("AGENT_HOOK_ENV[*]", "AGENT_HOOK_ENV[@]")
            if "AGENT_HOOK_ENV[@]" not in line:
                continue
            # `:+` and `+` are both set-tests and both safe; normalise so the
            # ratio does not redden a correct line and tell its author the
            # opposite of the truth.
            line = line.replace("AGENT_HOOK_ENV[@]:+", "AGENT_HOOK_ENV[@]+")
            # Length and key references never trip nounset -- ${#a[@]} is the
            # idiomatic pre-check before expanding -- so they are not value
            # expansions and must not be counted as one.
            line = re.sub(r"\$\{[#!]AGENT_HOOK_ENV\[@\]\}", "", line)
            # Counted, not searched. A substring test is per-LINE: one guarded
            # and one bare expansion on the same line passes, because the
            # guarded spelling itself supplies the `+`. The canonical form
            # ${AGENT_HOOK_ENV[@]+"${AGENT_HOOK_ENV[@]}"} carries exactly two
            # mentions per guard, so the ratio rejects a mixed line.
            #
            # No trailing-comment stripping: cutting at " #" is blind to
            # quoting, so an expansion after a `#` inside a string literal would
            # never be examined at all. The full-line skip above already covers
            # the prose in common.sh that named this array, and if someone later
            # writes a trailing comment mentioning it the cost is a loud test
            # failure, not a silent hole.
            # Floored, so a lone set-test `[ ${A[@]+x} ]` (one mention, one
            # guard) passes rather than failing a 1 == 2 comparison while being
            # the guard itself.
            bare = max(0, line.count("AGENT_HOOK_ENV[@]") - 2 * line.count("AGENT_HOOK_ENV[@]+"))
            assert bare == 0, (
                f"{script.name}:{n} expands AGENT_HOOK_ENV[@] without the `+` "
                "guard -- empty under `set -u` on the bash 3.2 macOS ships, so "
                "this passes here and breaks deploy on the operator's Mac"
            )


@pytest.mark.parametrize(
    ("parent", "colliding", "pinned"),
    [
        ("skills", "my-skill", True),
        ("skills", "growth/plow-invite", False),
        ("plugins", "plow-chat-platform", False),
    ],
    ids=["the-replay-publishes", "the-skill-retirement-removes", "the-plugin-retirement-removes"],
)
def test_a_planted_parent_symlink_cannot_redirect_the_install(
        run, instance, tmp_path, parent, colliding, pinned):
    """Neither seam that writes under the home may rm -rf or rename outside it.

    `plugins/` and `skills/` live in the home, which compose bind-mounts at
    the image's HERMES_HOME, so a compromised gateway can replace one with a
    symlink. The install then resolves through it and deletes host-side, as
    the operator -- and `--dest` being rejected by component does not cover a
    planted PARENT. Two seams walk that parent: the skills.tsv replay's
    fetch-tree, and the retirement of what older deploys staged, which runs
    first and refuses on its own.
    """
    from conftest import fake_docker, fake_skill_gh

    repo = instance("rowan")
    if pinned:
        (repo / "skills.tsv").write_text(f"plow-pbc/x\t{'a' * 40}\tmy-skill\t\n")
    run("register", "rowan", str(repo))
    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "home" / "not-the-agents"
    # At the name the seam touches, and asserted on CONTENT, not existence.
    # Both matter. A bystander file survives with or without the guard, since
    # nothing here is named for it. But so does a colliding path checked only for
    # existence: unguarded, `mv` renames this directory to .previous, the EXIT
    # trap `rm -rf`s it, and the freshly installed tree recreates the same path
    # with its own SKILL.md -- so the file "still exists" while the operator's
    # data is gone. The sentinel is what makes the assertion able to fail.
    sentinel = outside / colliding / "SKILL.md"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("name: my-skill\n# SENTINEL: the operator's own file\n")
    (home / parent).symlink_to("../not-the-agents")

    b = fake_skill_gh(tmp_path, skill_name="my-skill")
    d = fake_docker(tmp_path, home=home, name="rowan", home_env="/var/lib/hermes")
    r = run("deploy", "rowan", env={"PATH": f"{b}:{d}:{os.environ['PATH']}"})
    assert r.returncode != 0, "a seam followed a planted parent symlink"
    assert "outside" in r.stderr, f"refused, but not for this reason: {r.stderr}"
    assert "SENTINEL" in sentinel.read_text(), (
        "a seam renamed, replaced or removed a host directory outside the home"
    )
