import os
import pytest

from conftest import install_fake_gh
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_restore_installs_the_config_into_the_agents_home(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("restore", "rowan")
    assert r.returncode == 0, r.stderr
    installed = tmp_path / "home" / ".hermes-rowan" / "config.yaml"
    assert installed.exists()
    assert "openai-codex" in installed.read_text()


def test_restore_writes_a_dotenv_skeleton_carrying_both_platforms(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    env = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "PLOW_AGENT_TOKEN" in env
    assert "DOMO_MCP_TOKEN" in env, "latch is baseline, not an opt-in"


def test_restore_never_clobbers_an_existing_dotenv(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    env.write_text("PLOW_AGENT_TOKEN=real\n")
    run("restore", "rowan")
    assert env.read_text() == "PLOW_AGENT_TOKEN=real\n"


def test_migrate_plugin_env_copies_legacy_names_and_is_idempotent(run, instance, tmp_path):
    """The fleet migration step: legacy PLOW_CHAT_* values land under the names
    the unified plugin reads, the old lines stay (a pre-rename plugin still
    reads them mid-migration; a later cleanup removes them), and a second run
    writes nothing."""
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
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


@pytest.mark.parametrize("preexisting", [
    pytest.param("", id="fresh_dotenv"),
    # The stale row is the bug that shipped: activate's copy skipped set keys,
    # so re-activation left the PREVIOUS (dead) token under the current name.
    pytest.param("PLOW_AGENT_TOKEN=tok_stale\nPLOW_HOME_CHANNEL=cht_old\n", id="stale_current_values"),
])
def test_activate_syncs_legacy_names(run, instance, tmp_path, preexisting):
    """The pinned activate script is frozen and writes PLOW_CHAT_*; activate's
    post-step must SYNC those onto the current names — overwriting, because the
    script just minted the freshest values there are. A break here passes every
    other activate test: their stub scripts write no dotenv at all."""
    from conftest import fake_curl
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    env = tmp_path / "home" / ".hermes-rowan" / ".env"
    if preexisting:
        env.write_text(preexisting)
    (tmp_path / "act").mkdir()
    b = fake_curl(tmp_path / "act", body=(
        "#!/usr/bin/env bash\n"
        'd=""\n'
        'while [ $# -gt 0 ]; do case "$1" in --data-dir) d="$2"; shift 2 ;; *) shift ;; esac; done\n'
        "printf 'PLOW_CHAT_TOKEN=tok_live\\nPLOW_CHAT_CHAT_UID=cht_dm\\n' >> \"$d/.env\"\n"))
    r = run("activate", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    lines = env.read_text().splitlines()
    assert "PLOW_AGENT_TOKEN=tok_live" in lines
    assert "PLOW_HOME_CHANNEL=cht_dm" in lines
    assert "PLOW_CHAT_TOKEN=tok_live" in lines, "the pinned script's own lines must survive"
    assert "PLOW_AGENT_TOKEN=tok_stale" not in lines


def test_migrate_plugin_env_sync_overwrites_for_recovery(run, instance, tmp_path):
    """The recovery command activate prints must be able to finish the job.
    Idempotent mode skips set keys, so after a failed in-activate sync the
    fresh token sits only under the legacy name — `--sync` is the forwarded
    mode that overwrites."""
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
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
    run("restore", "rowan")
    r = run("migrate-plugin-env", "rowan", "--bogus")
    assert r.returncode != 0
    assert "unknown mode" in r.stderr and "--sync" in r.stderr


def test_migrate_plugin_env_without_a_dotenv_points_at_restore(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("migrate-plugin-env", "rowan")
    assert r.returncode != 0
    assert "restore" in r.stderr


def test_installed_state_is_not_reachable_by_other_users(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    for f in ("config.yaml", ".env"):
        mode = (tmp_path / "home" / ".hermes-rowan" / f).stat().st_mode
        assert not (mode & stat.S_IRWXO), f"{f} is reachable by other users"


def test_restore_on_an_instance_with_no_config_is_refused(run, instance):
    run("register", "bare", str(instance("bare", config=None)))
    r = run("restore", "bare")
    assert r.returncode != 0
    assert "config.yaml" in r.stderr


@pytest.mark.parametrize("pin", ["plow-chat-plugin.ref", "plow-chat-activate.ref",
                                 "google-workspace-skill.ref"])
def test_every_shipped_pin_is_a_sha_not_a_branch(pin):
    """A branch would silently re-point a running agent on the next upstream push.

    Both, because the activate pin gates the one command that is a one-time
    irreversible spend -- a branch name or a truncated SHA in that file would
    otherwise surface only when an operator ran it.
    """
    ref = (ROOT / "runtime" / pin).read_text().strip()
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)


def test_the_image_pin_is_a_digest_not_a_tag():
    ref = (ROOT / "runtime" / "image.ref").read_text().strip()
    assert ref.startswith("sha256:") and len(ref) == 71


@pytest.mark.parametrize(
    ("command", "env_key"),
    [
        ("install-plugin", "AGENT_MGR_PLUGIN_REF"),
        ("install-skill", "AGENT_MGR_SKILL_REF"),
    ],
)
def test_install_refuses_a_ref_that_is_not_a_sha(run, instance, command, env_key):
    """A branch would silently re-point every agent on the next upstream push --
    the same rule for the plugin pin and the fleet skill pin."""
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    r = run(command, "rowan", env={env_key: "main"})
    assert r.returncode != 0
    assert "40-char SHA" in r.stderr


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
    r = run("restore", "str")
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
    run("register", "str", str(instance("str", descriptor="AGENT_CONFIG=runtime/config.yaml\n",
                                        config=None)))
    r = run("restore", "str")
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
    run("restore", "str")
    env = (tmp_path / "home" / ".hermes-str" / ".env").read_text()
    assert "HOSTEX_TOKEN" in env and "SEAM_API_KEY" in env


def test_the_fleet_template_is_used_when_an_instance_ships_none(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    env = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "PLOW_AGENT_TOKEN" in env and "DOMO_MCP_TOKEN" in env


def test_restore_is_the_whole_deploy_including_the_instances_own_step(run, instance, tmp_path):
    """One command, one owner. The alternative -- agent-mgr doing its half and
    the README telling the operator to run the rest in order -- moves ownership
    to whoever reads the docs, which is not an owner at all."""
    repo = instance("str", descriptor="AGENT_RESTORE_HOOK=scripts/seed.sh\n")
    (repo / "scripts").mkdir()
    hook = repo / "scripts" / "seed.sh"
    hook.write_text(f'#!/usr/bin/env bash\ntouch {tmp_path / "hook-ran"}\n')
    hook.chmod(0o755)
    run("register", "str", str(repo))
    r = run("restore", "str")
    assert r.returncode == 0, r.stderr
    home = tmp_path / "home" / ".hermes-str"
    assert (home / "config.yaml").exists(), "config"
    assert (home / ".env").exists(), "dotenv skeleton"
    assert (tmp_path / "hook-ran").exists(), "the instance's own restore step never ran"


def test_a_failing_hook_fails_the_restore(run, instance, tmp_path):
    """A hook refuses for a reason -- a missing corpus, a failed composition.
    Swallowing it leaves the caller believing the deploy landed."""
    repo = instance("str", descriptor="AGENT_RESTORE_HOOK=scripts/seed.sh\n")
    (repo / "scripts").mkdir()
    hook = repo / "scripts" / "seed.sh"
    hook.write_text('#!/usr/bin/env bash\necho "no vault" >&2\nexit 1\n')
    hook.chmod(0o755)
    run("register", "str", str(repo))
    r = run("restore", "str")
    assert r.returncode != 0
    assert "ARE installed" in r.stderr and "is NOT" in r.stderr


def test_a_declared_hook_that_is_missing_is_named(run, instance):
    run("register", "str", str(instance("str", descriptor="AGENT_RESTORE_HOOK=scripts/gone.sh\n")))
    r = run("restore", "str")
    assert r.returncode != 0
    assert "restore hook" in r.stderr and "gone.sh" in r.stderr


def test_an_agent_with_no_hook_restores_fine(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    assert run("restore", "rowan").returncode == 0
    assert (tmp_path / "home" / ".hermes-rowan" / "config.yaml").exists()


def test_restore_installs_the_plugin_so_one_command_is_the_deploy(run, instance, tmp_path):
    """It used to be a second command the caller had to remember in order."""
    run("register", "rowan", str(instance("rowan")))
    plugin = tmp_path / "home" / ".hermes-rowan" / "plugins" / "plow-chat-platform"
    # Seed the layout a real pre-migration agent has, so the cleanup below is
    # something this test can fail on. Verified against the live homes on the
    # host: all three carry ref/hermes-plugin/plow_chat INSIDE
    # plugins/plow-chat-platform/, which is why a whole-directory swap clears
    # it. Asserting its absence against a home this test created fresh proved
    # nothing at all.
    legacy = plugin / "ref" / "hermes-plugin" / "plow_chat"
    legacy.mkdir(parents=True)
    (legacy / "adapter.py").write_text("# the old SEED layout\n")

    r = run("restore", "rowan")
    assert r.returncode == 0, r.stderr
    # The files, not the fetch. This used to assert that a faked `curl` ran,
    # which stopped meaning anything the moment the plugin started arriving
    # through fetch-tree -- and would have kept passing on a fetch that
    # installed nothing. What restore owes the caller is a plugin in the home.
    assert (plugin / "plugin.yaml").is_file(), "restore did not install the plugin manifest"
    assert (plugin / "__init__.py").is_file(), "restore did not install the adapter"
    assert "name: plow-chat-platform" in (plugin / "plugin.yaml").read_text()
    # Replace, not overlay: the swap is what retires the old SEED layout from
    # an agent's home. Overlaying would leave a second, stale copy of the
    # adapter in the directory the gateway enumerates.
    assert not (plugin / "ref").exists(), "the ref/ tree survived the install"

def _transition_env(tmp_path, log=None):
    from conftest import fake_docker
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan",
                    name="rowan", log=log)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _guarded(instance, run, tmp_path, *, refuses):
    """An instance whose pre-transition guard allows or refuses."""
    repo = instance("rowan", descriptor="AGENT_PRE_TRANSITION=scripts/guard.sh\n")
    (repo / "scripts").mkdir(exist_ok=True)
    g = repo / "scripts" / "guard.sh"
    g.write_text("#!/usr/bin/env bash\n"
                 + (f'echo "a nightly is mid-ingest" >&2\nexit 1\n' if refuses
                    else f'touch {tmp_path / "guard-ran"}\n'))
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
    for cmd in (["up", "rowan"], ["down", "rowan"], ["restart", "rowan"],
                ["compose", "rowan", "up", "-d", "--force-recreate"]):
        r = run(*cmd, env=env)
        assert r.returncode != 0, f"{cmd} transitioned past a refusing guard"
        assert "refused" in r.stderr


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
        "the operator was not told the activation succeeded, which is the whole point")


@pytest.mark.parametrize("args", [
    ("install-plugin", "rowan"),
    ("install-skill", "rowan"),
    ("sign-in", "rowan"),
    ("add-skill", "rowan", "plow-pbc/property-hunt", "--ref", "a" * 40),
])
def test_every_other_write_then_reload_still_fails_on_a_refused_guard(
        run, instance, tmp_path, args):
    """The negative half of `activate` being "the one command a refusal does not
    fail". These three are in the same position -- the write has landed by the
    reload -- so activate's `|| echo ...SUCCEEDED...` is the obvious next
    copy-paste, and it would make the word "one" false with a green suite."""
    import os
    _guarded(instance, run, tmp_path, refuses=True)
    from conftest import fake_docker, fake_skill_gh
    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True, exist_ok=True)
    # What each subcommand needs BEFORE its reload, so the refusal is what stops
    # it rather than a missing precondition: sign-in reads the installed config,
    # add-skill fetches a tarball. A RUNNING gateway for all three -- the reload
    # exits before the guard when there is none.
    (home / "config.yaml").write_text("model:\n  provider: openai-codex\n")
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
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_PRE_TRANSITION=scripts/gone.sh\n")))
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


@pytest.mark.parametrize("args", [
    ["up", "rowan"], ["down", "rowan"], ["restart", "rowan"],
    ["compose", "rowan", "up", "-d", "--force-recreate"],
    ["compose", "rowan", "start"], ["compose", "rowan", "pause"],
    ["compose", "rowan", "unpause"], ["compose", "rowan", "stop"],
])
def test_no_route_to_a_transition_bypasses_the_veto(run, instance, tmp_path, args):
    """Every route goes through compose_transition, so a new call site cannot
    forget the veto. start/pause/unpause interrupt a running process as surely
    as down does, and the earlier list omitted all three."""
    _guarded(instance, run, tmp_path, refuses=True)
    r = run(*args, env=_transition_env(tmp_path))
    assert r.returncode != 0, f"{args} transitioned past a refusing guard"
    assert "refused" in r.stderr


def test_a_reload_is_a_transition_too(run, instance, tmp_path):
    """restore writes and then reloads. Routing the reload around the veto let
    four write-then-reload subcommands restart the container mid-ingest."""
    repo = _guarded(instance, run, tmp_path, refuses=False)
    # Allow the restore's own pre-write veto, then refuse by the time it reloads.
    (repo / "scripts" / "guard.sh").write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(cat {tmp_path}/count 2>/dev/null || echo 0); echo $((n+1)) > {tmp_path}/count\n'
        '[ "$n" = 0 ] || { echo "a nightly started" >&2; exit 1; }\n')
    (repo / "scripts" / "guard.sh").chmod(0o755)
    r = run("restore", "rowan", env=_transition_env(tmp_path))
    assert r.returncode != 0
    assert "refused" in r.stderr


def test_the_subcommand_is_classified_not_the_flattened_argv(run, instance, tmp_path):
    """`up` can appear in a prompt, a filename or a flag value. Matching the
    flattened "$*" made those look like transitions."""
    _guarded(instance, run, tmp_path, refuses=True)
    r = run("compose", "rowan", "exec", "hermes", "echo", "please up the volume",
            env=_transition_env(tmp_path))
    assert r.returncode == 0, r.stderr


def test_restore_replays_every_pinned_skill(run, instance, tmp_path):
    """It is advertised as the whole deploy. A rebuild that omitted them left an
    agent whose skills.tsv said one thing and whose home held another."""
    from conftest import fake_docker, fake_skill_gh
    repo = instance("rowan")
    (repo / "skills.tsv").write_text(f"plow-pbc/x\t{'a' * 40}\tmy-skill\t\n")
    b = fake_skill_gh(tmp_path, skill_name="my-skill")
    # conftest's docker, which answers `config` -- the bare stub made
    # resolve-guard refuse at the reload, after the skill had installed.
    d = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    run("register", "rowan", str(repo))
    r = run("restore", "rowan", env={"PATH": f"{b}:{d}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "home" / ".hermes-rowan" / "skills" / "my-skill" / "SKILL.md").exists()


def test_a_missing_hook_is_caught_before_anything_is_written(run, instance, tmp_path):
    """Validated at the end, a missing hook left the plugin and config installed
    under a message saying the deploy did not land -- a report the state
    contradicts."""
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_RESTORE_HOOK=scripts/gone.sh\n")))
    r = run("restore", "rowan")
    assert r.returncode != 0
    assert "nothing was installed" in r.stderr
    assert not (tmp_path / "home" / ".hermes-rowan" / "config.yaml").exists()


def test_a_runtime_hook_failure_says_what_landed(run, instance, tmp_path):
    repo = _guarded(instance, run, tmp_path, refuses=False)
    (repo / "agent.env").write_text("AGENT_RESTORE_HOOK=scripts/hook.sh\n")
    h = repo / "scripts" / "hook.sh"
    h.write_text('#!/usr/bin/env bash\necho "no vault" >&2\nexit 1\n')
    h.chmod(0o755)
    r = run("restore", "rowan", env=_transition_env(tmp_path))
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
    j = next(n for n, l in enumerate(lines[i + 1:], i + 1) if l.rstrip() == end)
    return "\n".join(lines[i:j])


def test_each_pin_is_read_only_where_its_command_fetches():
    """The split exists to stop one ref serving two eras; this is that invariant.

    Both come from hermes-plow-chat, at two points in its history: `Strip the
    SEED ceremony` deleted ref/scripts/, so the plugin pin moves forward past it
    while create_plow_chat_curl.sh exists only before it. A ref read by the
    wrong command sends a post-strip SHA at a ref/scripts/ URL that does not
    exist at that commit -- and 404s on activate, the irreversible one.

    Per use-site, not over a concatenation of both files. The first version of
    this test asserted four substrings existed *somewhere* across common.sh and
    agent-mgr, which stayed green even if the two commands SWAPPED which pin
    they read -- precisely the failure it was written for.
    """
    plugin = _block((ROOT / "lib" / "common.sh").read_text(),
                    "install_plow_plugin()", "}")
    assert "plow-chat-plugin.ref" in plugin
    assert "plow-pbc/hermes-plow-chat" in plugin
    assert "plow-chat-activate.ref" not in plugin, "the plugin install must not read the activate pin"

    activate = _block((ROOT / "agent-mgr").read_text(), "    activate)", "    sign-in)")
    assert "plow-chat-activate.ref" in activate
    assert "plow-pbc/hermes-plow-chat" in activate
    assert "plow-chat-plugin.ref" not in activate, "activate must not read the plugin pin"


def test_the_activate_pin_is_frozen_and_distinct():
    """The activate ref may not be bumped at all, and this is what enforces it.

    Proving the ref is an ANCESTOR of the strip commit would need that repo's
    history, which is a network call this suite will not make. Pinning the SHA
    needs nothing, and reddens on every forward bump -- so the why lives in the
    failure message below, where whoever tripped it is already looking, rather
    than in a doc they would have to be sent to. The README's builds-on section
    is the same rule for someone reading before they bump.

    The inequality stays for the other direction: an edit writing one SHA into
    both files (a sed over runtime/, a copy-paste) installs the pre-strip layout
    as a plugin, and satisfies the equality above on its own.
    """
    plugin = (ROOT / "runtime" / "plow-chat-plugin.ref").read_text().strip()
    activate = (ROOT / "runtime" / "plow-chat-activate.ref").read_text().strip()
    assert activate == "98ddb2e7f0ce563a7ed6c9af43802d15b5ff62d3", (
        "the activate pin moved. It is frozen behind `Strip the SEED ceremony`, "
        "which deleted the ref/scripts/ path it names -- a later SHA 404s on "
        "activate. If this is deliberate, the new SHA must still predate that "
        "commit, and the README's builds-on section says why.")
    assert plugin != activate


def test_each_caller_says_what_landed_in_its_own_terms(run, instance, tmp_path):
    """restore and install-plugin leave the agent in opposite states on a failed fetch.

    One hard-coded sentence told the install-plugin operator their config and
    skills were gone, when install-plugin gates on an existing home and leaves
    both untouched -- and the obvious response to that message is to re-run
    restore over a healthy agent. Nothing pinned the split, so a revert to one
    sentence would have been silent.
    """
    import os
    b = tmp_path / "failing-bin"
    b.mkdir()
    (b / "gh").write_text("#!/usr/bin/env bash\nexit 1\n")
    (b / "gh").chmod(0o755)
    failing = {"PATH": f"{b}:{os.environ['PATH']}"}

    run("register", "rowan", str(instance("rowan")))
    r = run("restore", "rowan", env=failing)
    assert r.returncode != 0
    assert "gh auth status" in r.stderr, "the gh diagnosis must survive"
    assert "config.yaml and skills are NOT" in r.stderr

    # A home restore already made, so install-plugin gets past its own gate.
    run("restore", "rowan")
    r = run("install-plugin", "rowan", env=failing)
    assert r.returncode != 0
    assert "gh auth status" in r.stderr
    assert "untouched" in r.stderr
    assert "are NOT" not in r.stderr, "install-plugin must not claim the config is gone"


def test_the_plugin_install_refuses_a_caller_that_does_not_say_what_landed(tmp_path):
    """The contract is checked at entry, so a forgetful caller fails immediately.

    Pinned because the difference is invisible from the two real call sites --
    both pass a sentence, so moving ${1:?...} out of the die string and into a
    `local` binding changes nothing they can observe. What it changes is the
    forgetful caller: inside the die it was expanded only on the failure branch,
    and bash then aborted on the expansion BEFORE die ran, replacing the gh-auth
    diagnosis with "1: the caller must say what landed" at the one moment the
    diagnosis was needed. This calls it with no argument against a SUCCEEDING
    fetch -- which returns 0 silently under the old form.
    """
    import os
    import subprocess

    home = tmp_path / "home" / ".hermes-probe"
    home.mkdir(parents=True)
    b = tmp_path / "bin"
    b.mkdir()
    install_fake_gh(tmp_path, b)

    script = f"""
    set -euo pipefail
    AGENT_MGR_ROOT={ROOT}
    AGENT_HOME={home}
    AGENT_NAME=probe
    die() {{ echo "$*" >&2; exit 1; }}
    . {ROOT}/lib/common.sh
    install_plow_plugin
    """
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, "a caller that says nothing about state must not succeed"
    assert "the caller must say what landed" in r.stderr


def test_an_orphaned_tree_from_a_killed_run_does_not_survive_the_next_install(
        run, instance, tmp_path):
    """A killed run leaves a valid second plugin tree where the gateway looks.

    The trap does not fire on SIGKILL, an OOM kill or a power loss, so the
    staging and backup directories can outlive their run. `.previous` is the
    sharp one: it is a COMPLETE tree carrying `name: plow-chat-platform`, beside
    the real one, in the directory the gateway enumerates.

    Both names used to be pid-suffixed, which meant a run only ever cleaned up
    after its own pid-twin -- every other orphan stayed forever. And the
    backup's own rm sits inside the `is there a current install` branch, so on a
    first install that branch is skipped and the orphan survives untouched.
    Seeded here with NO current install, which is the case that got missed.
    """
    run("register", "rowan", str(instance("rowan")))
    plugins = tmp_path / "home" / ".hermes-rowan" / "plugins"
    orphan = plugins / "plow-chat-platform.previous"
    orphan.mkdir(parents=True)
    (orphan / "plugin.yaml").write_text("name: plow-chat-platform\n")
    (plugins / "plow-chat-platform.incoming").mkdir()

    r = run("restore", "rowan")
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in plugins.iterdir()) == ["plow-chat-platform"], \
        "an orphaned tree survived the install"


def test_a_rollback_copy_is_promoted_before_the_next_run_can_fail(
        run, instance, tmp_path):
    """An install that fails after an interrupted one still leaves a valid tree.

    `find` is stubbed because it drives the chmod pass -- the first fallible
    step after the recovery point.
    """
    run("register", "rowan", str(instance("rowan")))
    plugins = tmp_path / "home" / ".hermes-rowan" / "plugins"
    rollback = plugins / "plow-chat-platform.previous"
    rollback.mkdir(parents=True)
    (rollback / "plugin.yaml").write_text("name: plow-chat-platform\n")

    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "find").write_text("#!/usr/bin/env bash\nexit 1\n")
    (b / "find").chmod(0o755)

    r = run("restore", "rowan")
    assert r.returncode != 0, "the stubbed find should have failed this install"
    assert (plugins / "plow-chat-platform" / "plugin.yaml").is_file(), \
        "the rollback copy was cleared before the fallible step, leaving no tree"


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
        # yet, which a first restore needs, so bare `realpath "$p"` IS the #19
        # break. The lookbehind is what skips os.path.realpath( in a python3 -c.
        "realpath": r"(?<![\w.])realpath\b",
    }
    scripts = [ROOT / "agent-mgr"] + sorted((ROOT / "lib").iterdir())
    for script in scripts:
        # Full-line comments only. common.sh explains why realpath is NOT used,
        # and that sentence must not trip the check it is documenting.
        code = "\n".join(l for l in script.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        for tool, pattern in banned.items():
            assert not re.search(pattern, code), (
                f"{script.name} uses {tool}, which is not portable to the "
                "macOS 12.3 floor README commits to -- the suite runs on Linux, "
                "so it lands green here and fails on the operator's Mac")


def test_the_possibly_empty_array_is_always_expansion_guarded():
    """`${AGENT_HOOK_ENV[@]}` must never be expanded without its `+` guard.

    The array is empty for an agent with no extra descriptor keys, and bash
    before 4.4 -- which macOS ships -- treats that as unset under `set -u`, so
    the bare expansion kills `restore`. common.sh owns why; this pins that the
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
            bare = max(0, line.count("AGENT_HOOK_ENV[@]")
                          - 2 * line.count("AGENT_HOOK_ENV[@]+"))
            assert bare == 0, (
                f"{script.name}:{n} expands AGENT_HOOK_ENV[@] without the `+` "
                "guard -- empty under `set -u` on the bash 3.2 macOS ships, so "
                "this passes here and breaks restore on the operator's Mac")


def test_a_planted_parent_symlink_cannot_redirect_the_install(run, instance, tmp_path):
    """The publication seam must not rm -rf or rename outside the agent's home.

    `plugins/` and `skills/` live in the home, which compose bind-mounts at
    /opt/data, so a compromised gateway can replace one with a symlink. The
    install then resolves through it and deletes host-side, as the operator --
    and `--dest` being rejected by component does not cover a planted PARENT.
    """
    run("register", "rowan", str(instance("rowan")))
    home = tmp_path / "home" / ".hermes-rowan"
    home.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "home" / "not-the-agents"
    # At the name the installer touches, and asserted on CONTENT, not existence.
    # Both matter. A bystander file survives with or without the guard, since
    # nothing here is named for it. But so does a colliding path checked only for
    # existence: unguarded, `mv` renames this directory to .previous, the EXIT
    # trap `rm -rf`s it, and the freshly installed tree recreates the same path
    # with its own plugin.yaml -- so the file "still exists" while the operator's
    # data is gone. The sentinel is what makes the assertion able to fail.
    (outside / "plow-chat-platform").mkdir(parents=True)
    (outside / "plow-chat-platform" / "plugin.yaml").write_text(
        "name: plow-chat-platform\n# SENTINEL: the operator's own file\n")
    (home / "plugins").symlink_to("../not-the-agents")

    r = run("restore", "rowan")
    assert r.returncode != 0, "the install followed a planted parent symlink"
    assert "outside" in r.stderr, f"refused, but not for this reason: {r.stderr}"
    assert "SENTINEL" in (outside / "plow-chat-platform" / "plugin.yaml").read_text(), \
        "the install renamed or replaced a host directory outside the home"
