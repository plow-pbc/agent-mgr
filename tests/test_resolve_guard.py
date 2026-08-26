"""The guard proves what Compose actually resolved, rather than trusting that
unsetting the descriptor keys held.

The refusal cases stub the mismatch rather than producing it through a real
`compose.override.yml`. What is under test is the guard's reaction to Compose
disagreeing with the descriptor, not Compose's merge -- and the suite runs with
the real docker shadowed, because a fixture agent named `rowan` or `str`
resolves to the LIVE compose project (plow-pbc/agent-mgr#13).
"""
import os
import sys

import pytest

from conftest import fake_docker


def _mismatched(tmp_path, name, **kw):
    """A docker whose resolved config disagrees with the descriptor."""
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, **kw)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def _agent(run, instance, name, descriptor=""):
    repo = instance(name, descriptor=descriptor)
    run("register", name, str(repo))
    return repo


def test_the_guard_passes_when_the_resolved_config_matches(run, instance):
    _agent(run, instance, "rowan")
    r = run("resolve-guard", "rowan")
    assert r.returncode == 0, r.stderr + r.stdout


def test_the_guard_refuses_when_an_override_retargets_the_home(run, instance, tmp_path):
    """An override that mounts a different home at /opt/data must be caught, even
    though every descriptor variable resolved exactly as written."""
    _agent(run, instance, "rowan")
    env = _mismatched(tmp_path, "rowan")
    # The mismatch: Compose resolves a different home at /opt/data.
    (tmp_path / "bin" / "docker").write_text(
        (tmp_path / "bin" / "docker").read_text().replace(
            str(tmp_path / "home" / ".hermes-rowan"), str(tmp_path / ".hermes-SOMEONE-ELSE")))
    r = run("resolve-guard", "rowan", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_the_guard_refuses_when_an_override_renames_the_container(run, instance, tmp_path):
    _agent(run, instance, "rowan")
    env = _mismatched(tmp_path, "rowan", container="hermes")
    r = run("resolve-guard", "rowan", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_a_stale_compose_project_name_cannot_re_project_the_stack(run, instance):
    """COMPOSE_PROJECT_NAME outranks the template's `name:`. container_name and
    the home both still resolve correctly, so only an explicit project check
    catches it -- and without it `up` builds a stack under a foreign project
    against this agent's live home while `down` reports nothing to stop."""
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve-guard", "rowan", env={"COMPOSE_PROJECT_NAME": "someone-elses-project"})
    assert r.returncode == 0, "the stale value must be unset, not merely detected: " + r.stderr


def test_an_override_cannot_re_project_the_stack_at_all(run, instance):
    """`-p` outranks every other source of the project name, so an override that
    sets `name:` is ignored rather than caught. Prevention, not detection: there
    is no path by which this agent's stack lands under another project."""
    repo = instance("rowan")
    run("register", "rowan", str(repo))
    (repo / "compose.override.yml").write_text("name: someone-elses-project\n")
    r = run("resolve-guard", "rowan")
    assert r.returncode == 0, r.stderr
    assert "someone-elses-project" not in run("resolve", "rowan").stdout


def _retargeting(instance, run, name, tmp_path, config=None):
    """Registered and restored, then handed a docker that resolves someone
    else's home.

    The restore runs against an AGREEING docker first: `restore` now consults
    resolve-guard before it writes, so a retargeting fake would refuse the setup
    rather than the command under test."""
    repo = instance(name) if config is None else instance(name, config=config)
    run("register", name, str(repo))
    ok = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name)
    run("restore", name, env={"PATH": f"{ok}:{os.environ['PATH']}"})
    b = fake_docker(tmp_path, home=tmp_path / ".hermes-SOMEONE-ELSE", name=name)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def test_sign_in_will_not_write_a_credential_through_a_retargeting_override(run, instance, tmp_path):
    """sign-in mutates a running stack. Reaching Compose without the guard let a
    credential write land against a sibling agent's mounted home."""
    env = _retargeting(instance, run, "rowan", tmp_path)
    r = run("sign-in", "rowan", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_check_latch_will_not_probe_through_a_retargeting_override(run, instance, tmp_path):
    # A config that declares latch, so the probe gets past the not-configured
    # exit and actually reaches the guard this test is about.
    env = _retargeting(instance, run, "property", tmp_path,
                       config="model:\n  provider: openai-codex\nmcp_servers:\n  latch:\n"
                              "    url: https://api.plow.co/v1/relay/devices/x/mcp\n")
    (tmp_path / "home" / ".hermes-property" / ".env").write_text(
        "DOMO_DEVICE_UID=dev_1\nDOMO_MCP_TOKEN=tok_1\n")
    r = run("check-latch", "property", env=env)
    assert r.returncode != 0
    assert "refusing to act" in r.stderr


def test_the_guard_refuses_cleanly_when_compose_cannot_produce_a_config(run, instance, tmp_path):
    """A refusal, not a traceback: the operator needs to know the guard stopped
    them, not how it is implemented."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "docker").write_text("#!/usr/bin/env bash\necho 'not json'\nexit 0\n")
    (b / "docker").chmod(0o755)
    run("register", "rowan", str(instance("rowan")))
    import os
    r = run("resolve-guard", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "refusing to act" in r.stderr
    assert "Traceback" not in r.stderr


def _sibling_home(instance, run, name, tmp_path):
    """A descriptor copied from a sibling: self-consistent, and wrong."""
    repo = instance(name, descriptor=f"AGENT_HOME={tmp_path}/home/.hermes-rowan\n")
    run("register", name, str(repo))
    return repo


def test_restore_will_not_write_into_a_siblings_home(run, instance, tmp_path):
    """resolve-guard proves Compose agrees with the descriptor, which a copied
    descriptor naming a sibling's home satisfies perfectly. restore never goes
    near Compose, so only the ownership check catches it."""
    _sibling_home(instance, run, "property", tmp_path)
    r = run("restore", "property")
    assert r.returncode != 0
    assert "not property's own home" in r.stderr


def test_install_plugin_will_not_write_into_a_siblings_home(run, instance, tmp_path):
    _sibling_home(instance, run, "property", tmp_path)
    r = run("install-plugin", "property")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_add_skill_will_not_write_into_a_siblings_home(run, instance, tmp_path):
    _sibling_home(instance, run, "property", tmp_path)
    r = run("add-skill", "property", "plow-pbc/x", "--ref", "a" * 40, "--dest", "s")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_the_legacy_bare_home_is_still_allowed_when_declared(run, instance, tmp_path):
    """The rentals agent predates the convention; an explicit declaration is
    deliberate, and the convention can never produce a bare .hermes."""
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    r = run("restore", "str")
    assert r.returncode == 0, r.stderr


def test_two_agents_may_not_share_a_home(run, instance, tmp_path):
    """The check that actually closes the legacy exception. A descriptor copied
    from the rentals agent declares its bare `.hermes` and satisfies any
    name-shape test -- self-consistent and wrong. The registry sees it."""
    legacy = tmp_path / "home" / ".hermes"
    legacy.mkdir(parents=True, exist_ok=True)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    run("register", "copycat", str(instance("copycat", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    r = run("restore", "copycat")
    assert r.returncode != 0
    assert "str is already registered there" in r.stderr


def test_the_agent_that_declared_it_first_still_works(run, instance, tmp_path):
    (tmp_path / "home" / ".hermes").mkdir(parents=True, exist_ok=True)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    run("register", "rowan", str(instance("rowan")))
    assert run("restore", "str").returncode == 0


def test_sign_in_will_not_mint_into_a_siblings_home(run, instance, tmp_path):
    """It writes a credential into the home exactly as activate does."""
    run("register", "rowan", str(instance("rowan")))
    run("register", "property",
        str(instance("property", descriptor=f"AGENT_HOME={tmp_path}/home/.hermes-rowan\n")))
    r = run("sign-in", "property")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_a_siblings_single_quoted_home_still_collides(run, instance, tmp_path):
    """The collision check used to run its own descriptor parser, which stripped
    only double quotes -- so a sibling declaring AGENT_HOME='$HOME/.hermes'
    compared unequal to the same path and the collision it exists to catch went
    through. One resolver now, so both spellings resolve identically."""
    run("register", "str", str(instance("str", descriptor="AGENT_HOME='$HOME/.hermes'\n")))
    run("register", "rowan", str(instance("rowan", descriptor='AGENT_HOME="$HOME/.hermes"\n')))
    r = run("restore", "rowan")
    assert r.returncode != 0, "a second agent claimed a home a sibling already declares"
    assert "str is already registered there" in r.stderr



def test_an_unresolvable_sibling_does_not_open_the_legacy_home(run, instance, tmp_path):
    """The one arm that rests on the collision check having been complete. `str`
    owns the bare `.hermes`; move its repo and it stops resolving, so a copycat
    declaring the same home would otherwise pass and write config and
    credentials into a live agent's mounted home. Skipping the row silently
    turned the only fail-closed check here into a fail-open one."""
    import shutil
    str_repo = instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")
    run("register", "str", str(str_repo))
    shutil.rmtree(str_repo)
    run("register", "copycat", str(instance("copycat", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    r = run("restore", "copycat")
    assert r.returncode != 0, "a copycat claimed a live agent's home through a stale row"
    assert "could not be resolved" in r.stderr
    assert "could not resolve str" in r.stderr, "the skipped row was not named"




def test_the_refusal_carries_the_real_reason_and_the_right_remedy(run, instance, tmp_path):
    """load_agent refuses a present, healthy, RUNNING agent whose descriptor
    fails validation just as readily as one whose repo is gone -- an unreadable
    agent.env in a repo that is still there. Telling that operator to unregister
    a live agent is worse than saying nothing, so the reason has to reach
    them."""
    # Present repo, unreadable descriptor: load_agent refuses it, but the agent
    # is still there, so 'unregister' would be the wrong thing to tell anyone.
    bad = instance("bad")
    (bad / "agent.env").unlink()
    (bad / "agent.env").mkdir()
    run("register", "bad", str(bad))
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    r = run("restore", "str")
    assert r.returncode != 0
    assert "could not resolve bad" in r.stderr, "the skipped sibling was not named"
    assert "Fix that descriptor if the agent is still there" in r.stderr, (
        "an operator whose sibling is alive was told to unregister it")

def test_two_conventional_homes_aliasing_one_directory_collide(run, instance, tmp_path):
    """The case that invalidated the old invariant, on the shape where the
    collision loop is load-bearing: both names conventional, both symlinked to
    one directory, so the name test cannot tell them apart."""
    target = tmp_path / "srv" / "shared"
    target.mkdir(parents=True)
    home = tmp_path / "home"; home.mkdir(exist_ok=True)
    (home / ".hermes-rowan").symlink_to(target)
    (home / ".hermes-copycat").symlink_to(target)
    run("register", "rowan", str(instance("rowan")))
    run("register", "copycat", str(instance("copycat")))
    r = run("restore", "copycat")
    assert r.returncode != 0, "two conventional names reached one directory undetected"
    assert "rowan is already registered there" in r.stderr


@pytest.mark.parametrize(("name", "descriptor"), [
    ("str", "AGENT_HOME=$HOME/.hermes\n"),
    ("plain", ""),
])
def test_an_unresolvable_sibling_refuses_every_home(run, instance, name, descriptor):
    """Both shapes, one contract: an incomplete collision set is not trusted,
    and the remedy named in the refusal actually clears it.

    Not narrowed to homes that "could alias" -- three attempts at that proxy
    were each wrong in a new direction, because aliasing is a relation between
    two paths and nothing about this home says anything about the home of a
    sibling we could not resolve.
    """
    import shutil
    dead = instance("dead")
    run("register", "dead", str(dead))
    shutil.rmtree(dead)
    run("register", name, str(instance(name, descriptor=descriptor)))

    r = run("restore", name)
    assert r.returncode != 0, "an incomplete collision set was trusted"
    assert "cannot prove no one else claims that home" in r.stderr
    assert "could not resolve dead" in r.stderr, "the skipped row was not named"

    assert run("unregister", "dead").returncode == 0
    assert run("restore", name).returncode == 0, "unregister did not clear it"


@pytest.mark.parametrize(("kw", "refused", "why", "expect"), [
    ({"image": "nousresearch/hermes-agent:latest"}, True,
     "a pulled tag re-resolves on the next pull, and this container holds the "
     "agent's credentials", "neither a digest nor built here"),
    ({"build": True, "pull_policy": "never"}, False,
     "the rentals agent's shape, once it declares it will not fetch", ""),
    ({"build": True, "image": "nousresearch/hermes-agent:latest",
      "pull_policy": "never"}, False,
     "a built service is exempt whatever it is NAMED -- two attempts to derive "
     "safety from the reference string were both wrong. It is exempt here "
     "because of the pull_policy on THIS row, not because of the passthrough: "
     "that closes the other door, and neither is sufficient alone", ""),
    ({}, False, "the fleet-wide digest", ""),
    ({"build": True, "pull_policy": "missing"}, True,
     "`missing` PULLS when the local tag is absent -- the earlier probe said "
     "otherwise only because its registry was unresolvable and the failed pull "
     "fell back to the build", "pull_policy is 'missing', and only 'never' or 'build'"),
    ({"build": True, "pull_policy": "always"}, True,
     "and a policy that refetches IS the hole -- it fetches over the top of "
     "what this host built", "pull_policy is 'always', and only 'never' or 'build'"),
    ({"build": True, "pull_policy": "refresh"}, True,
     "a real Compose policy that refetches and was absent from the denylist -- "
     "which is why the arm is an allowlist", "pull_policy is 'refresh', and only 'never' or 'build'"),
    ({"build": True, "pull_policy": "build"}, False,
     "`build` leaves a built image alone", ""),
    ({"build": True}, True,
     "no policy at all is the default, and the default pulls",
     "pull_policy is unset (the default, which pulls)"),
    ({"build": True, "pull_policy": "daily"}, True,
     "the periodic policies refetch like `always`", "pull_policy is 'daily', and only 'never' or 'build'"),
    ({"build": True, "pull_policy": "every_12h"}, True,
     "including the parameterised one", "pull_policy is 'every_12h', and only 'never' or 'build'"),
])
def test_the_image_rule_reads_what_compose_resolved(
        run, instance, tmp_path, kw, refused, why, expect):
    """On the resolved-Compose seam, not the descriptor variable. Checking
    AGENT_IMAGE in load_agent was wrong in both directions: it refused the
    supported `build:` shape outright, while an override replacing
    `hermes.image` sailed past it."""
    import os
    from conftest import fake_docker
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan", **kw)
    r = run("resolve-guard", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    if refused:
        assert r.returncode != 0, f"accepted a mutable image: {why}"
        # Each row names ITS refusal: the image rule and the build-policy arm
        # give different messages, and one shared assertion let a row pass on
        # the other's.
        assert expect in r.stderr, f"refused, but not for the reason under test: {r.stderr}"
    else:
        assert r.returncode == 0, f"refused a legitimate image ({why}): {r.stderr}"


def test_restore_refuses_a_bad_image_before_it_writes_anything(run, instance, tmp_path):
    """The image rule lives on resolve-guard, which `restore` used to reach only
    through reload-if-running on its LAST line -- so a deploy would install
    config, the plugin and every pinned skill and refuse afterwards, having
    already done the thing the refusal exists to prevent."""
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    image="nousresearch/hermes-agent:latest")
    r = run("restore", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "neither a digest nor built here" in r.stderr
    # The whole home, not just config.yaml -- which is the FOURTH thing restore
    # writes, after the mkdir, the .env skeleton and the plugin install. A test
    # named "before it writes anything" has to mean it.
    assert not (tmp_path / "home" / ".hermes-rowan").exists(), (
        "the deploy created the home before refusing")


@pytest.mark.parametrize("args", [
    ("install-plugin", "rowan"),
    ("add-skill", "rowan", "plow-pbc/x", "--ref", "a" * 40),
])
def test_every_write_command_preflights_the_image(run, instance, tmp_path, args):
    """restore was fixed and its two siblings kept the shape it was fixed for:
    both reached resolve-guard only through reload-if-running on their last
    line, so the plugin or the skill landed in the mounted credential home and
    the refusal came after."""
    run("register", "rowan", str(instance("rowan")))
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    image="nousresearch/hermes-agent:latest")
    (tmp_path / "home" / ".hermes-rowan").mkdir(parents=True)
    r = run(*args, env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0, f"{args[0]} ran against an unpinned image"
    assert "neither a digest nor built here" in r.stderr
    assert not list((tmp_path / "home" / ".hermes-rowan").iterdir()), (
        f"{args[0]} wrote into the home before refusing")


def test_a_digest_is_exempt_from_the_fetch_policy(run, instance, tmp_path):
    """A digest names one immutable image, so any policy refetches the same
    thing -- gating it would refuse the very remedy the build arm recommends."""
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan",
                    pull_policy="always")
    run("register", "rowan", str(instance("rowan")))
    assert run("resolve-guard", "rowan",
               env={"PATH": f"{b}:{os.environ['PATH']}"}).returncode == 0, (
        "a pinned digest was refused for a policy that cannot change it")


def test_a_resolver_that_fails_refuses_instead_of_passing(run, instance, tmp_path):
    """The ownership guard compares two resolved paths, so a resolver that
    cannot run left "" on both sides, matched, and let the write through. That
    is why the refusal is spelled `|| die` and not left to `set -e`: every
    write-then-reload subcommand reaches this code from the left of a `||` in
    lib/reload-if-running, where bash suspends `set -e` for the whole call tree.

    What this pins is the refusal an operator sees. Without it the command still
    exits 1 here -- but with EMPTY stderr, which tells them nothing about a
    resolver that a re-run would likely get past.

    The stub fails only `canonical_path`'s realpath and lets `normalized_path`'s
    abspath through, which is what a per-call failure looks like -- a fork the
    host would not give, an interpreter that crashed. Removing python3 outright
    would not reach this: load_agent resolves through it first and refuses
    earlier, for a different reason.
    """
    b = tmp_path / "stub-bin"
    b.mkdir()
    # Matched on the full call and on the qualified name, not on `$2` and a
    # bare `realpath`: argument position and a loose word would both make this
    # stub silently start killing some other python3 call -- lib/resolve-guard
    # runs one too. `os.path.realpath(` is canonical_path's alone, and there is
    # a note beside it in lib/common.sh saying so.
    (b / "python3").write_text(
        "#!/bin/sh\n"
        'case "$*" in *"os.path.realpath("*) exit 1 ;; esac\n'
        f'exec {sys.executable} "$@"\n'
    )
    (b / "python3").chmod(0o755)

    run("register", "rowan", str(instance("rowan")))
    r = run("restore", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    # The whole refusal, not just "could not resolve": that phrase appears in
    # every one of these guards, so a looser assertion stays green if the
    # refusal relocates between them -- which is the failure this pins.
    assert "refusing to write to" in r.stderr
    assert "could not resolve" in r.stderr
