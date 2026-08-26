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
    # Refused rather than skipped: the key is malformed, and a malformed
    # declaration is a repository error.
    assert r.returncode != 0
    assert "malformed" in r.stderr


@pytest.mark.parametrize("line", [
    "STR VAULT=x",             # a key this tool does not own, with a space in it
    "AGENT_TZ AGENT_IMAGE=x",  # multi-token: matched the allowlist as a substring
])
def test_a_malformed_declaration_is_refused(run, instance, line):
    """Refused, not classified.

    Every one of these is invalid dotenv, and Compose rejects the same lines
    through --env-file -- so tolerating any would let agent-mgr and Compose
    disagree about one file. Dying is also the only behaviour that cannot go
    wrong quietly; the alternative was a second partial grammar here, sorting
    near-misses of an owned key from malformed unowned lines, to write a better
    message for a line nobody should have written.
    """
    run("register", "rowan", str(instance("rowan", descriptor=f"{line}\n")))
    r = run("resolve", "rowan")
    assert r.returncode != 0, f"accepted a malformed declaration: {line!r}"
    assert "malformed key" in r.stderr


@pytest.mark.parametrize("line", [
    "# AGENT_TZ=America/Chicago is the default",
    "  # AGENT_TZ=America/Chicago is the default",   # indented, and carries an =
    "\t# commented out: AGENT_IMAGE=pinned",
    "   ",
])
def test_comments_and_blanks_are_skipped_however_indented(run, instance, line):
    """A commented-out setting usually carries an `=`, and a comment indented
    inside a block is ordinary -- Compose skips both. Refusing one as a
    malformed key would make a descriptor agent-mgr's own template could contain
    unreadable."""
    run("register", "rowan", str(instance("rowan", descriptor=f"{line}\nAGENT_TZ=UTC\n")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert "AGENT_TZ=UTC" in r.stdout


def test_one_repos_malformed_key_blocks_writes_on_every_other_agent(run, instance, tmp_path):
    """The amplification, pinned so the trade-off is deliberate rather than
    discovered.

    `require_own_home` is fail-closed by design: it refuses when it cannot prove
    no sibling claims this home, and a descriptor it cannot parse is exactly
    that case. Refusing a malformed key therefore reaches past the repo that
    contains it -- a stray space in agent B blocks direct-write commands on
    agent A.

    Kept rather than softened: the alternative is guessing a home out of a
    descriptor that could not be read, which is the fail-open this guard exists
    to prevent. The refusal names the sibling, the reason and the remedy, which
    is what makes it survivable.
    """
    run("register", "rowan", str(instance("rowan")))
    run("register", "broken", str(instance("broken", descriptor="AGENT_TZ AGENT_IMAGE=x\n")))
    r = run("restore", "rowan")
    assert r.returncode != 0
    assert "could not resolve broken" in r.stderr
    assert "malformed key" in r.stderr
    assert "unregister broken" in r.stderr, "the refusal must name the way out"


@pytest.mark.parametrize("line,expected", [
    ("AGENT_TZ = America/Chicago", "America/Chicago"),        # spaces around =
    ("  AGENT_TZ=America/Chicago", "America/Chicago"),        # indented
    ("\tAGENT_TZ=America/Chicago", "America/Chicago"),        # tab-indented
    ("export AGENT_TZ=America/Chicago", "America/Chicago"),   # shell-style export
    ("export\tAGENT_TZ=America/Chicago", "America/Chicago"),  # export + tab
    ("AGENT_TZ=America/Chicago   ", "America/Chicago"),       # trailing space: trimmed
    ("AGENT_TZ =  America/Chicago  ", "America/Chicago"),     # both sides
    ('AGENT_TZ="America/Chicago  "', "America/Chicago  "),    # quoted: spaces KEPT
    ("AGENT_TZ=America/Chicago # the default", "America/Chicago"),   # inline comment
    ("AGENT_TZ=America/Chicago# no space", "America/Chicago# no space"),  # not a comment
    ('AGENT_TZ="America/Chicago # kept"', "America/Chicago # kept"),      # quotes protect
    ("AGENT_TZ='America/Chicago # kept'", "America/Chicago # kept"),
    ('AGENT_TZ="America/Chicago" trailing', "America/Chicago"),  # dropped after close quote
])
def test_the_spellings_compose_accepts_are_accepted_here(run, instance, line, expected):
    """Parity with compose-go, measured rather than assumed.

    Compose reads this same file through --env-file, and a real
    `docker compose --env-file` accepts all five of these and reads them as the
    bare key. Refusing them here would make agent-mgr fail on a descriptor
    Compose reads without complaint -- and through require_own_home's
    fail-closed arm, fail every OTHER registered agent's direct-write commands
    over one repo's indentation.
    """
    run("register", "rowan", str(instance("rowan", descriptor=f"{line}\n")))
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    # Exact line, not a substring: a substring check cannot tell a normalized
    # value from one carrying trailing whitespace, which is the half that was
    # unpinned when this test was written.
    assert f"AGENT_TZ={expected}\n" in r.stdout


@pytest.mark.parametrize("line", [
    'AGENT_TZ="America/Chicago',
    "AGENT_TZ='America/Chicago",
    'AGENT_TZ="America/Chicago\'',
])
def test_an_unterminated_quote_is_refused(run, instance, line):
    """Compose refuses these, so accepting them is the dangerous direction:
    the descriptor would resolve here and break the deploy that reads it."""
    run("register", "rowan", str(instance("rowan", descriptor=f"{line}\n")))
    r = run("resolve", "rowan")
    assert r.returncode != 0, f"accepted an unterminated quote: {line!r}"
    assert "unterminated quote" in r.stderr


def _home_env(tmp_path, name, text):
    """The dotenv the operator already keeps per person, mounted at /opt/data."""
    h = tmp_path / "home" / f".hermes-{name}"
    h.mkdir(parents=True, exist_ok=True)
    (h / ".env").write_text(text)
    return h / ".env"


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
    # Warned and skipped, NOT fatal. Compose never reads this file, so there is
    # no parity to keep -- and killing every command for every agent over a
    # stray line in somebody's hand-maintained credential file is out of all
    # proportion to it. The descriptor, which Compose DOES read, still refuses.
    assert r.returncode == 0, r.stderr
    # names the line and the KIND of error, quotes nothing from a file that
    # sits beside credentials
    assert "line 1: malformed key" in r.stderr and "ignoring it" in r.stderr
    assert "AGENT_T[" not in r.stderr
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout


def test_an_unterminated_final_line_is_still_read(run, instance, tmp_path):
    """The dotenv is maintained by hand and by the gateway, so a last line with
    no trailing newline is ordinary. `read` returns non-zero at EOF even having
    filled $line, so without the guard the zone silently falls back."""
    run("register", "rowan", str(instance("rowan")))
    h = tmp_path / "home" / ".hermes-rowan"
    h.mkdir(parents=True, exist_ok=True)
    (h / ".env").write_text("AGENT_TZ=America/Chicago")  # no trailing newline
    assert "AGENT_TZ=America/Chicago" in run("resolve", "rowan").stdout


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


def test_a_broken_dotenv_does_not_take_down_other_agents(run, instance, tmp_path):
    """The asymmetry, pinned.

    A malformed line in a DESCRIPTOR is fatal, and through require_own_home's
    fail-closed arm that reaches every other registered agent -- deliberate,
    because Compose reads that file and a descriptor it cannot parse is one this
    tool cannot reason about.

    A dotenv is not that file. Compose never reads it, it is hand-maintained
    beside credentials, and the only thing agent-mgr wants from it is a
    timezone. A stray line there must cost that instance its zone and nothing
    else -- not its own commands, and certainly not anybody else's.
    """
    run("register", "rowan", str(instance("rowan")))
    run("register", "other", str(instance("other")))
    _home_env(tmp_path, "rowan", "AGENT_T[oops]=1\n")
    r = run("resolve", "rowan")
    assert r.returncode == 0, r.stderr
    assert "AGENT_TZ=America/Los_Angeles" in r.stdout
    # restore, not resolve: require_own_home runs on direct-write commands only,
    # so `resolve` cannot observe the amplification this test exists to deny.
    other = run("restore", "other")
    assert other.returncode == 0, other.stderr
    assert "could not resolve rowan" not in other.stderr
