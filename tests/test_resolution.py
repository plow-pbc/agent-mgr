import os
import pathlib
import pytest

from conftest import fake_docker

def test_home_defaults_to_the_conventional_path(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes-rowan" in r.stdout
    assert "AGENT_CONTAINER=hermes-rowan" in r.stdout
    assert "AGENT_PROJECT=hermes-rowan" in r.stdout


def test_the_image_defaults_to_the_fleet_wide_pinned_digest(run, instance):
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan")
    assert "AGENT_IMAGE=nousresearch/hermes-agent@sha256:" in r.stdout


def test_a_descriptor_override_wins_over_the_convention(run, instance):
    repo = instance("str", descriptor="AGENT_HOME=/opt/legacy/.hermes\nAGENT_CONTAINER=hermes\n")
    run("register", "str", str(repo))
    r = run("resolve", "str")
    assert "AGENT_HOME=/opt/legacy/.hermes" in r.stdout
    assert "AGENT_CONTAINER=hermes" in r.stdout


def test_a_descriptor_may_expand_home(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")
    run("register", "str", str(repo))
    r = run("resolve", "str")
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes\n" in r.stdout


def test_a_stale_shell_variable_cannot_retarget_the_agent(run, instance):
    """Compose resolves shell env ahead of --env-file. The descriptor must win."""
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan", env={"AGENT_HOME": "/home/odio/.hermes-WRONG"})
    assert "/home/odio/.hermes-WRONG" not in r.stdout
    assert ".hermes-rowan" in r.stdout


def test_a_stale_shell_container_name_cannot_retarget_the_agent(run, instance):
    run("register", "rowan", str(instance("rowan")))
    r = run("resolve", "rowan", env={"AGENT_CONTAINER": "hermes"})
    assert "AGENT_CONTAINER=hermes-rowan" in r.stdout


def test_an_unregistered_agent_is_refused_by_name(run):
    r = run("resolve", "ghost")
    assert r.returncode != 0
    assert "ghost" in r.stderr and "not registered" in r.stderr


def test_an_instance_repo_with_no_descriptor_is_refused(run, tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    run("register", "bare", str(repo))
    r = run("resolve", "bare")
    assert r.returncode != 0
    assert "agent.env" in r.stderr


def test_resolve_with_no_name_asks_which_agent(run):
    r = run("resolve")
    assert r.returncode != 0
    assert "which agent" in r.stderr


def test_a_descriptor_is_read_not_executed(run, instance, tmp_path):
    """The descriptor is documented as declarative. Dot-sourcing it made any
    registered repo able to run arbitrary commands with the operator's
    credentials the moment `resolve` touched it -- before any Compose guard."""
    canary = tmp_path / "pwned"
    repo = instance("rowan", descriptor=f'AGENT_TZ=$(touch {canary})\n')
    run("register", "rowan", str(repo))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert not canary.exists(), "the descriptor executed a command substitution"
    assert "AGENT_TZ=$(touch" in r.stdout, "the value should stay literal"


def test_a_descriptor_cannot_run_a_command_through_a_later_line(run, instance, tmp_path):
    """A parser that only *starts* strict still executes if it falls back to
    sourcing on anything it does not recognise."""
    canary = tmp_path / "pwned2"
    repo = instance("rowan", descriptor=f'AGENT_TZ=UTC\ntouch {canary}\nAGENT_CONTAINER=x\n')
    run("register", "rowan", str(repo))
    r = run("resolve", "rowan")
    assert not canary.exists(), "a non-assignment line was executed"
    assert "AGENT_TZ=UTC" in r.stdout


def test_a_descriptor_cannot_set_the_loader_or_the_command_lookup(run, instance):
    """A PATH from a descriptor would reach every docker, curl and gh this tool
    runs afterwards -- the shell execution the parse exists to prevent, by a
    different door. Held by the allowlist rather than a denylist: only
    AGENT_KEYS reaches this process, so the dangerous names do not have to be
    enumerated in advance."""
    repo = instance("rowan", descriptor="PATH=/tmp/evil\nLD_PRELOAD=/tmp/x.so\nAGENT_TZ=UTC\n")
    run("register", "rowan", str(repo))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert "/tmp/evil" not in r.stdout
    assert "AGENT_TZ=UTC" in r.stdout, "the rest of the descriptor still applies"


def test_home_expansion_still_works_because_the_template_documents_it(run, instance, tmp_path):
    repo = instance("str", descriptor="AGENT_HOME=${HOME}/.hermes\n")
    run("register", "str", str(repo))
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes\n" in run("resolve", "str").stdout


def test_an_instances_own_variables_reach_its_hook(run, instance, tmp_path):
    """An override-only variable like STR_VAULT is what an instance's compose
    override and its restore hook are written against. Exporting it here is what
    lets the hook stop keeping a second copy of a path the descriptor owns."""
    out = tmp_path / "seen"
    repo = instance("str", descriptor=f"STR_VAULT=$HOME/hermes-vault\nAGENT_RESTORE_HOOK=h.sh\n")
    hook = repo / "h.sh"
    hook.write_text(f'#!/usr/bin/env bash\nprintf "%s" "$STR_VAULT" > {out}\n')
    hook.chmod(0o755)
    run("register", "str", str(repo))
    r = run("restore", "str")
    assert r.returncode == 0, r.stderr
    assert out.read_text() == f"{tmp_path / 'home'}/hermes-vault"


def test_a_non_agent_variable_is_still_parsed_not_executed(run, instance, tmp_path):
    """Widening the parser past AGENT_* must not widen it into a shell."""
    canary = tmp_path / "pwned3"
    run("register", "str", str(instance("str", descriptor=f'STR_VAULT=$(touch {canary})\n')))
    r = run("resolve", "str")
    assert r.returncode == 0, r.stderr
    assert not canary.exists()


def test_a_descriptor_cannot_repoint_the_tool_at_its_own_code(run, instance, tmp_path):
    """AGENT_MGR_ROOT decides where lib/ is loaded from. Exporting every
    descriptor key handed a registered repo the dispatcher: ordinary lifecycle
    commands would run that repo's resolve-guard with the operator's
    credentials. An allowlist closes it; a denylist cannot, because the
    dangerous names are whatever this tool happens to read."""
    evil = tmp_path / "evil"
    (evil / "lib").mkdir(parents=True)
    canary = tmp_path / "pwned-root"
    (evil / "lib" / "resolve-guard").write_text(f"#!/usr/bin/env bash\ntouch {canary}\n")
    (evil / "lib" / "resolve-guard").chmod(0o755)
    run("register", "rowan", str(instance("rowan", descriptor=f"AGENT_MGR_ROOT={evil}\n")))
    import os
    from conftest import fake_docker
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    run("resolve", "rowan")
    # `up` runs resolve-guard, which is the file the descriptor tried to supply.
    run("up", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert not canary.exists(), "a descriptor repointed AGENT_MGR_ROOT"


def test_an_instance_variable_still_reaches_its_hook(run, instance, tmp_path):
    """Narrowing the export must not take the hooks' own environment with it."""
    seen = tmp_path / "seen"
    repo = instance("str", descriptor="STR_VAULT=/tmp/v\nAGENT_PRE_TRANSITION=g.sh\n")
    g = repo / "g.sh"
    g.write_text(f'#!/usr/bin/env bash\nprintf "%s" "$STR_VAULT" > {seen}\n')
    g.chmod(0o755)
    run("register", "str", str(repo))
    import os
    from conftest import fake_docker
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-str", name="str",
                    container="hermes-str", project="hermes-str")
    run("up", "str", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert seen.read_text() == "/tmp/v"





# --- the descriptor parser is data, not shell -------------------------------


@pytest.fixture
def injection_marker():
    """A marker path containing NO '-'.

    Load-bearing. The exploit needs the key's bracket expression to be a VALID
    character class, and a '-' between two characters inside one is a range --
    `[$(touch /tmp/pytest-of-odio/...)Z]` is an invalid range, so grep errors
    and the attack silently fails to reproduce. pytest's tmp_path always
    contains '-', so a marker under it makes this test pass against the
    vulnerable code: green for the wrong reason.
    """
    d = pathlib.Path(f"/tmp/agentmgr_inj_{os.getpid()}")
    d.mkdir(exist_ok=True)
    marker = d / "executed"
    marker.unlink(missing_ok=True)
    yield marker
    marker.unlink(missing_ok=True)
    d.rmdir()


def test_a_descriptor_key_cannot_execute_host_code(run, instance, injection_marker):
    """Read, never execute -- the property this parser exists for.

    The membership check ran $key as a grep PATTERN, so a bracket expression
    matched an allowlisted name as a character class (`AGENT_T[...Z]` matches
    AGENT_TZ). The name then reached `printf -v`, where bash evaluates an array
    subscript arithmetically and arithmetic performs command substitution -- so
    any `agent-mgr resolve` on a registered repo ran that repo's code, as the
    operator.
    """
    key = f"AGENT_T[$(touch {injection_marker})Z]"
    run("register", "rowan", str(instance("rowan", descriptor=f"{key}=1\n")))
    r = run("resolve", "rowan")
    assert not injection_marker.exists(), (
        "a descriptor key executed host code -- the parser is a shell again"
    )
    assert r.returncode == 0, r.stderr
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout


def test_only_a_declared_key_reaches_the_assignment(run, instance):
    """`grep -w` matches a word-bounded SUBSTRING of the space-joined allowlist,
    so a multi-token key is an exact match: `AGENT_TZ AGENT_IMAGE` is inside
    AGENT_KEYS. It reached `printf -v`, which rejects the name, and under set -e
    that killed load_agent -- every subcommand for the agent died on a raw bash
    error instead of the documented path.
    """
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_TZ AGENT_IMAGE=x\n")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, f"the CLI died on a malformed key: {r.stderr}"
    assert "not a valid identifier" not in r.stderr
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout
    assert "AGENT_IMAGE=nousresearch/hermes-agent@sha256:" in r.stdout


def test_a_malformed_spelling_of_an_owned_key_is_reported(run, instance):
    """`AGENT_TZ = x` is the common hand-written form and is not valid dotenv.

    Refusing it is right -- Compose reads the same file through --env-file with
    a parser that rejects it too. Refusing it *silently* is not: the value falls
    back to the default and looks exactly like a line never written.
    """
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_TZ = America/Chicago\n")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert "malformed" in r.stderr and "AGENT_TZ" in r.stderr
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout


def test_an_unowned_malformed_key_stays_quiet(run, instance):
    """This tool does not own STR_VAULT, so it has no standing to comment on
    how that repo spells it."""
    run("register", "rowan", str(instance("rowan", descriptor="STR VAULT=x\n")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert r.stderr.strip() == ""


@pytest.mark.parametrize("spelling", [
    "AGENT_TZ = America/Chicago",   # spaces around =
    "  AGENT_TZ=America/Chicago",   # indented
    "export AGENT_TZ=America/Chicago",  # shell-style export
    "export\tAGENT_TZ=America/Chicago",  # export + tab, not one space
    "\tAGENT_TZ=America/Chicago",        # tab-indented
])
def test_every_near_miss_of_an_owned_key_is_reported(run, instance, spelling):
    """Three ways to almost-declare a key this tool owns, all invalid dotenv.

    Compose's --env-file rejects all three too, so accepting any would make the
    two disagree about one file. Dropping them silently is the failure: the
    value falls back to its default and looks like a line never written.
    """
    run("register", "rowan", str(instance("rowan", descriptor=f"{spelling}\n")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert "malformed" in r.stderr and "AGENT_TZ" in r.stderr
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout


# --- per-instance timezone, from the instance's own dotenv ------------------


def _home_env(tmp_path, name, text):
    """The dotenv the operator already keeps per person, mounted at /opt/data."""
    h = tmp_path / "home" / f".hermes-{name}"
    h.mkdir(parents=True, exist_ok=True)
    (h / ".env").write_text(text)
    return h / ".env"


def test_no_dotenv_changes_nothing(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    before = run("resolve", "rowan").stdout
    _home_env(tmp_path, "someone-else", "AGENT_TZ=America/Chicago\n")
    assert run("resolve", "rowan").stdout == before


def test_the_dotenv_sets_this_instances_zone(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    _home_env(tmp_path, "rowan", "AGENT_TZ=America/Chicago\n")
    assert "AGENT_TZ=America/Chicago" in run("resolve", "rowan").stdout


def test_the_dotenv_beats_the_shared_descriptor(run, instance, tmp_path):
    """The whole point: one repo, several people, different clocks."""
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_TZ=America/Los_Angeles\n")))
    _home_env(tmp_path, "rowan", "AGENT_TZ=America/Chicago\n")
    out = run("resolve", "rowan").stdout
    assert "AGENT_TZ=America/Chicago" in out
    assert "America/Los_Angeles" not in out


def test_two_instances_of_one_repo_take_their_own_zones(run, instance, tmp_path):
    """One checkout, two registry rows, two dotenvs -- the shape this exists for."""
    repo = str(instance("life"))
    run("register", "life", repo)
    run("register", "rowan", repo)
    _home_env(tmp_path, "rowan", "AGENT_TZ=America/Chicago\n")
    life, rowan = run("resolve", "life").stdout, run("resolve", "rowan").stdout
    assert "AGENT_TZ=America/Los_Angeles" in life and "/.hermes-life" in life
    assert "AGENT_TZ=America/Chicago" in rowan and "/.hermes-rowan" in rowan


def test_the_dotenv_cannot_set_anything_but_the_zone(run, instance, tmp_path):
    """It holds credentials. Exactly one non-secret value is taken from it, and
    identity is already derived before it is read, so it cannot move its home."""
    run("register", "rowan", str(instance("rowan")))
    _home_env(tmp_path, "rowan",
              "AGENT_TZ=America/Chicago\nAGENT_HOME=/opt/hijack\n"
              "AGENT_IMAGE=pinned:by-hand\nPLOW_CHAT_TOKEN=sk-secret\n")
    r = run("resolve", "rowan")
    assert "AGENT_TZ=America/Chicago" in r.stdout
    assert "/opt/hijack" not in r.stdout
    assert "pinned:by-hand" not in r.stdout
    assert "sk-secret" not in r.stdout
    assert f"AGENT_HOME={tmp_path / 'home'}/.hermes-rowan" in r.stdout


def test_the_dotenv_is_read_never_executed(run, instance, tmp_path, injection_marker):
    """Same parser, so the same contract -- a value cannot reach $(...)."""
    run("register", "rowan", str(instance("rowan")))
    _home_env(tmp_path, "rowan", f"AGENT_T[$(touch {injection_marker})Z]=1\n")
    r = run("resolve", "rowan")
    assert not injection_marker.exists()
    assert r.returncode == 0, r.stderr


def test_the_dotenv_zone_reaches_compose(run, instance, tmp_path):
    """The whole chain, end to end -- the only test that proves the feature.

    Three hops, and any two green while the third is broken says nothing:
    load_agent must READ the dotenv, EXPORT the resolved AGENT_TZ, and Compose
    must prefer that export over the --env-file it is handed (which is the
    SHARED descriptor, carrying somebody else's zone). `resolve` reads ${!k} and
    never needs the export; a rendering test that injects AGENT_TZ by hand never
    runs the read.

    So this runs the real CLI against a stub docker that reports the AGENT_TZ it
    was handed. Deleting AGENT_TZ from the export list, or the dotenv read,
    fails here and nowhere else.
    """
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_TZ=America/Los_Angeles\n")))
    _home_env(tmp_path, "rowan", "AGENT_TZ=America/Chicago\n")

    seen = tmp_path / "seen-tz"
    b = fake_docker(tmp_path, home=tmp_path / "home" / ".hermes-rowan", name="rowan")
    stub = b / "docker"
    stub.write_text(stub.read_text().replace(
        "#!/usr/bin/env bash",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "${{AGENT_TZ-<unset>}}" >> {seen}', 1))

    r = run("compose", "rowan", "config", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    assert seen.exists(), "the stub docker never ran -- the chain was not exercised"
    saw = seen.read_text()
    assert "America/Chicago" in saw, (
        f"compose was handed {saw.strip()!r}; the dotenv's zone did not reach it, "
        f"so the container would run on the shared descriptor's clock"
    )
    assert "America/Los_Angeles" not in saw


def test_an_unterminated_final_line_is_still_read(run, instance, tmp_path):
    """The dotenv is maintained by hand and by the gateway, so a last line with
    no trailing newline is ordinary. `read` returns non-zero at EOF even having
    filled $line, so without the guard the zone silently falls back."""
    run("register", "rowan", str(instance("rowan")))
    h = tmp_path / "home" / ".hermes-rowan"
    h.mkdir(parents=True, exist_ok=True)
    (h / ".env").write_text("AGENT_TZ=America/Chicago")  # no trailing newline
    assert "AGENT_TZ=America/Chicago" in run("resolve", "rowan").stdout


def test_an_unterminated_descriptor_line_is_still_read(run, instance):
    """Same guard, the other file."""
    run("register", "rowan", str(instance("rowan", descriptor="AGENT_TZ=America/Chicago")))
    assert "AGENT_TZ=America/Chicago" in run("resolve", "rowan").stdout


@pytest.mark.parametrize("line,expect", [
    # A key the dotenv MAY set, spelled wrong -> tell them the spelling.
    ("AGENT_TZ = America/Chicago", "malformed"),
    ("export\tAGENT_TZ=America/Chicago", "malformed"),
    # A key agent-mgr owns but this file may NOT set -> a spelling fix would be
    # useless, so say where it belongs instead.
    ("export AGENT_IMAGE=pinned", "this file may set only"),
    ("AGENT_HOME = /opt/hijack", "this file may set only"),
])
def test_the_dotenv_says_which_kind_of_wrong_a_key_is(run, instance, tmp_path, line, expect):
    """Two different mistakes with two different remedies.

    Reachable only from the dotenv call site, where the allowlist is narrower
    than AGENT_KEYS -- from the descriptor the two collapse and the second
    branch cannot fire.
    """
    run("register", "rowan", str(instance("rowan")))
    _home_env(tmp_path, "rowan", f"{line}\n")
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert expect in r.stderr
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout
    assert "hijack" not in r.stdout and "pinned" not in r.stdout
