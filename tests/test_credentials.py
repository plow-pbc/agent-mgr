import os
import shutil
import sys
import stat
import subprocess

import pytest
from conftest import ROOT, LATCH_CONFIG, fake_docker


def _fake_docker(tmp_path, name="rowan"):
    log = tmp_path / "argv.log"
    b = fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{name}", name=name, log=log)
    return b, log


def test_sign_in_authenticates_against_the_installed_config_not_the_repo_copy(run, instance, tmp_path):
    """The gateway resolved model.provider from the INSTALLED config at boot.
    Reading the repo copy would mint a credential for a provider it is not using
    the moment the two differ."""
    run("register", "rowan", str(instance("rowan", config="model:\n  provider: openai-codex\n")))
    run("restore", "rowan")
    (tmp_path / "home" / ".hermes-rowan" / "config.yaml").write_text("model:\n  provider: anthropic\n")
    b, log = _fake_docker(tmp_path)
    r = run("sign-in", "rowan", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    calls = log.read_text()
    assert "auth add anthropic" in calls
    assert "auth add openai-codex" not in calls


def test_sign_in_refuses_before_restore_has_run(run, instance):
    run("register", "rowan", str(instance("rowan")))
    r = run("sign-in", "rowan")
    assert r.returncode != 0
    assert "restore" in r.stderr


@pytest.mark.parametrize("command", ["activate", "set-latch"])
@pytest.mark.parametrize(
    "descriptor",
    ["AGENT_HOME=/etc\n", "AGENT_HOME=/tmp/.hermes-property\n"],
    ids=["not-a-hermes-home", "a-siblings-conventional-home"],
)
def test_credential_writers_refuse_a_home_that_is_not_this_agents(run, instance, command, descriptor):
    """Every command that writes a credential into a home takes the same guard.
    Pointed at a sibling's, activate would take that agent off its chat and spend
    a one-time activation; set-latch would hand it a relay credential minted
    against someone else's Mac."""
    run("register", "rowan", str(instance("rowan", descriptor=descriptor)))
    r = run(command, "rowan", input="dev_abc\ntok_xyz\n")
    assert r.returncode != 0
    assert "refusing to write" in r.stderr


def test_activate_allows_a_legacy_bare_home_the_descriptor_declared(run, instance, tmp_path):
    """The rentals agent predates the ~/.hermes-<name> convention. An explicit
    declaration is deliberate; the convention can never produce a bare .hermes."""
    legacy = tmp_path / "home" / ".hermes"
    legacy.mkdir(parents=True)
    run("register", "str", str(instance("str", descriptor="AGENT_HOME=$HOME/.hermes\n")))
    # ACTIVATE_REF, not PLUGIN_REF. activate reads its own pin now, so the
    # plugin override stopped reaching it -- the command ran on through curl,
    # bash and reload-if-running, which also dragged a hermetic test onto the
    # host's real docker daemon. And the surviving assertion matched a string
    # that appears nowhere in the tool, so it could not have failed either way.
    r = run("activate", "str", env={"AGENT_MGR_ACTIVATE_REF": "not-a-sha"})
    # It gets past the home guard and fails later, on the ref -- which is the
    # proof that the guard let it through. Asserted on what the tool prints.
    assert r.returncode != 0
    assert "40-char SHA" in r.stderr
    # The string the guard actually prints. Two other tests in this file pin the
    # same one; the previous two spellings here matched nothing any code emits,
    # so the line could not fail either way.
    assert "refusing to write" not in r.stderr


# The two axes are independent, so a product would run redundant CLIs. One row
# per dotenv shape, with the padded stdin -- the axis that pins the value strip
# -- on one of them.
@pytest.mark.parametrize(
    "starting_dotenv,preserved",
    [
        (b"HOSTEX_TOKEN=keep-me\nDOMO_DEVICE_UID=\nDOMO_MCP_TOKEN=\n", (b"HOSTEX_TOKEN=keep-me",)),
        # No DOMO_* at all -- the append arm.
        (b"HOSTEX_TOKEN=keep-me\n", (b"HOSTEX_TOKEN=keep-me",)),
        # Two canonical declarations, which is what appending a line at the
        # bottom produces. The upsert must leave exactly one, no stale value.
        (b"HOSTEX_TOKEN=keep-me\nDOMO_MCP_TOKEN=stale\nDOMO_MCP_TOKEN=staler\n",
         (b"HOSTEX_TOKEN=keep-me",)),
        # Bytes this command does not own, and THREE independent mechanisms,
        # each of which would cut or corrupt a credential it must only copy:
        #   \xe9   -- becomes U+FFFD if the file is decoded to edit it
        #   \xc2\x85 -- a line break to str-level splitting, not to the gateway
        #   \r     -- a line break to bytes.splitlines(), not to the gateway
        # They are independent, so each needs its own byte: a surrogateescape
        # implementation survives the first and cuts the second, and
        # bytes.splitlines() survives both and cuts the third.
        (b"SEAM_API_KEY=caf\xe9-la\rtin1\nHOSTEX_TOKEN=a\xc2\x85b\nDOMO_MCP_TOKEN=\n",
         (b"SEAM_API_KEY=caf\xe9-la\rtin1", b"HOSTEX_TOKEN=a\xc2\x85b")),
        # No terminating newline -- a hand-edited file, or an editor configured
        # not to add one. Every other row ends in \n, so the trailing-newline
        # conditional in upsert() is never driven through its False side; make
        # the pop unconditional and this row loses HOSTEX_TOKEN entirely,
        # republished at 0600 as a well-formed file with set-latch exiting 0.
        (b"HOSTEX_TOKEN=keep-me", (b"HOSTEX_TOKEN=keep-me",)),
    ],
    ids=["pre-seeded-empty", "absent", "canonical-duplicate", "bytes-we-do-not-own",
         "no-trailing-newline"],
)
@pytest.mark.parametrize(
    "stdin",
    [
        "dev_abc\ntok_xyz\n",
        # What a paste actually looks like -- the axis that pins the strip.
        "  dev_abc \n\ttok_xyz  \n",
    ],
    ids=["clean", "padded"],
)
def test_set_latch_writes_the_pair_and_carries_every_other_key_through(run, instance, tmp_path, starting_dotenv, preserved, stdin):
    """The dotenv is shared -- the rentals agent keeps a PMS token and a lock API
    key in the same file -- so an upsert that rewrote the file would take those
    with it. And whatever spelling a key arrives in, exactly one declaration may
    survive: two readers of this file disagree about which of a pair is live."""
    run("register", "rowan", str(instance("rowan", config=LATCH_CONFIG)))
    run("restore", "rowan")
    env_file = tmp_path / "home" / ".hermes-rowan" / ".env"
    env_file.write_bytes(starting_dotenv)
    b, _ = _fake_docker(tmp_path)
    r = run("set-latch", "rowan", input=stdin,
            env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr
    body = env_file.read_bytes()
    # The whole LINE, at column 0 -- not a substring. A future edit that carried
    # the incoming line's `export ` or indent through would satisfy a substring
    # match and produce exactly the spelling the readers have to agree about.
    # In bytes, because a key this command does not own may not be valid UTF-8.
    assert b"DOMO_DEVICE_UID=dev_abc" in body.split(b"\n")
    assert b"DOMO_MCP_TOKEN=tok_xyz" in body.split(b"\n")
    # The exact surviving set, in order -- not membership, which is satisfied
    # however many OTHER lines are present and so cannot see an ADDITION.
    #
    # Empties are asserted rather than filtered. A `if l` guard drops every
    # blank, which silently exempts one of the additions this is here to catch:
    # a stray blank from joining an already-terminated body. Dropping the
    # trailing-newline pop in upsert() produces exactly that, and the suite
    # stayed green on it until this line counted the terminator instead.
    lines = body.split(b"\n")
    assert lines.pop() == b"", "the dotenv must end in exactly one newline"
    # By owned key, not by a DOMO_ prefix: a user's own DOMO_REGION would
    # otherwise be exempted from the set this is meant to be guarding.
    survivors = tuple(l for l in lines
                      if l.split(b"=", 1)[0] not in (b"DOMO_DEVICE_UID", b"DOMO_MCP_TOKEN"))
    assert survivors == preserved, "a line this command does not own was rewritten or added"
    # One declaration each, in any spelling -- not a second appended beside the
    # one it was meant to replace, and no stale value left underneath.
    assert body.count(b"DOMO_MCP_TOKEN=") == 1
    assert body.count(b"DOMO_DEVICE_UID=") == 1
    assert b"stale" not in body
    # The dotenv holds live credentials and the home is on a shared host.
    assert (env_file.stat().st_mode & 0o777) == 0o600
    # No staging left in either place. The home is bind-mounted, so a leftover
    # there is also a name a gateway could later swap; the parent is where the
    # replacement is staged precisely so no such name exists inside the mount.
    home_dir = env_file.parent
    assert not list(home_dir.glob("*.set-latch.*"))
    assert not list(home_dir.glob(".env.*"))
    assert not list(home_dir.parent.glob("*.set-latch.*"))
    # Never the whole token, on either stream -- the operator may be screen-sharing.
    assert "tok_xyz" not in r.stdout
    assert "tok_xyz" not in r.stderr
    # But the last three must be the STORED value's, not the raw paste's. For a
    # padded paste the raw tail is "z  " -- trailing spaces are invisible in a
    # terminal, so the operator reads "...z" here and "...xyz" from check-latch's
    # REVOKED line, and two spellings of one credential is what sends them to
    # revoke a live token.
    assert "...xyz" in r.stdout


def test_set_latch_refuses_an_agent_whose_config_declares_no_latch(run, instance):
    """A pair written for an agent with no latch is a credential no gateway ever
    reads, sitting in a dotenv looking like working configuration."""
    run("register", "rowan", str(instance("rowan")))
    run("restore", "rowan")
    r = run("set-latch", "rowan", input="dev_abc\ntok_xyz\n")
    assert r.returncode != 0
    assert "declares no latch" in r.stderr


@pytest.mark.parametrize(
    "stdin,missing",
    [
        ("dev_abc\n\n", "DOMO_MCP_TOKEN"),
        # Empty is the PARSER's empty, not the shell's: `[ -n "   " ]` is true,
        # so this used to be written out as a key the gateway loads as nothing.
        ("dev_abc\n   \n", "DOMO_MCP_TOKEN"),
        ("   \ntok_xyz\n", "DOMO_DEVICE_UID"),
    ],
    ids=["token-blank", "token-whitespace", "uid-whitespace"],
)
def test_set_latch_refuses_an_empty_value_rather_than_writing_it(run, instance, tmp_path, stdin, missing):
    """An empty write is the half-configured state check-latch exists to report
    -- manufactured by the command meant to prevent it. Refused before anything
    reaches the dotenv, so there is nothing to undo."""
    run("register", "rowan", str(instance("rowan", config=LATCH_CONFIG)))
    run("restore", "rowan")
    r = run("set-latch", "rowan", input=stdin)
    assert r.returncode != 0
    assert f"{missing} was empty" in r.stderr
    body = (tmp_path / "home" / ".hermes-rowan" / ".env").read_text()
    assert "dev_abc" not in body
    assert "tok_xyz" not in body


@pytest.mark.parametrize(
    "plant,expected",
    [
        # Which layer speaks differs, so the message does too. A symlink passes
        # the `-f` gate and is stopped on the write path, which owes the
        # did-not-half-happen line. A FIFO is not a regular file, so `-f` turns
        # it away first and owes only its own diagnosis.
        ("symlink-out", ["cannot read", "Nothing was written"]),
        # Named with the .env path, because eight `die` sites in this file share
        # "run 'agent-mgr restore" -- two of them inside set-latch. A bare
        # fragment would be satisfied by the config.yaml gate at :237, so a
        # setup regression that never reached the plant would keep this green.
        ("fifo", [".env -- run 'agent-mgr restore"]),
    ],
)
def test_set_latch_will_not_read_a_dotenv_the_gateway_swapped(run, instance, tmp_path, plant, expected):
    """The home is a live container mount, and the agents that most need a latch
    read attacker-controlled input. A gateway that got out of hand can swap the
    dotenv for a symlink to any file the operator can read: following it would
    copy that file's contents back inside the mount, which is an exfiltration
    path out of the host, not a bad write.

    The open is O_NOFOLLOW relative to a directory FD on the home, so the kernel
    refuses it -- there is no resolve-then-use window for a link planted after a
    check. Asserted: that it refused, that stderr carries the diagnosis owed by
    whichever layer turned it away -- the write path owes the did-not-half-happen
    line, the `-f` gate owes only its own -- and that the host file was neither
    read into the home nor replaced, which is the one that actually says the
    secret stayed out."""
    secret = tmp_path / "host-only-secret"
    secret.write_text("BEGIN OPENSSH PRIVATE KEY\n")
    run("register", "rowan", str(instance("rowan", config=LATCH_CONFIG)))
    run("restore", "rowan")
    env_file = tmp_path / "home" / ".hermes-rowan" / ".env"
    env_file.unlink()
    if plant == "symlink-out":
        env_file.symlink_to(secret)
    else:
        os.mkfifo(env_file)
    r = run("set-latch", "rowan", input="dev_abc\ntok_xyz\n")
    assert r.returncode != 0
    # Not just non-zero. Under `set -euo pipefail` the script aborts anyway, so
    # a returncode assertion alone leaves both `|| die` clauses deletable with
    # the suite green -- and what the operator loses is the line saying the
    # write did not half-happen, which is what decides whether they go re-mint
    # and revoke a live credential.
    for fragment in expected:
        assert fragment in r.stderr
    # Untouched: the publish never ran, so nothing materialised in the mount.
    assert env_file.is_symlink() or stat.S_ISFIFO(env_file.stat().st_mode)
    assert secret.read_text() == "BEGIN OPENSSH PRIVATE KEY\n"
    # And the credential did not reach the host file either.
    assert "tok_xyz" not in secret.read_text()


def test_set_latch_accepts_a_home_symlinked_onto_another_disk(run, instance, tmp_path):
    """The accept side, and the reason only the LEAF is opened no-follow. Putting
    agent state on the big disk is ordinary here -- load_agent keeps AGENT_HOME
    normalized rather than resolved for exactly that reason -- and the home's
    own parents are operator-owned, not container-owned. Refusing a symlinked
    home would send the operator back to hand-editing the dotenv, which is what
    this command was added to stop, and the refusal test alone stays green
    through that.

    Mirrors test_a_home_symlinked_onto_another_disk_still_works, which guards the
    same setup for restore."""
    target = tmp_path / "srv" / "rowan"
    target.mkdir(parents=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "home" / ".hermes-rowan").symlink_to(target)
    run("register", "rowan", str(instance("rowan", config=LATCH_CONFIG)))
    run("restore", "rowan")
    r = run("set-latch", "rowan", input="dev_abc\ntok_xyz\n")
    assert r.returncode == 0, f"a symlinked home was refused: {r.stderr}"
    assert "DOMO_MCP_TOKEN=tok_xyz" in (target / ".env").read_text().splitlines()


def test_set_latch_env_reads_only_a_regular_leaf(tmp_path):
    """Straight at the helper, because set-latch's `-f` gate hides this one: a
    FIFO is not a regular file, so the gate turns it away before the helper is
    reached. The symlink direction needs no row here -- set-latch's own
    swapped-dotenv test covers it through the production path.

    O_NOFOLLOW rules out a symlink and nothing else. A FIFO planted at the leaf
    would park the open forever, a wedged command with no diagnostic, which is
    worse than a refusal."""
    home = tmp_path / "home"
    home.mkdir()
    os.mkfifo(home / ".env")
    r = subprocess.run([str(ROOT / "lib" / "set-latch-env"), str(home)],
                       input="dev_abc\ntok_xyz\n", capture_output=True, text=True, timeout=10)
    assert r.returncode != 0
    assert "not a regular file" in r.stderr


