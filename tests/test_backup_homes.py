"""What `backup-homes` must not silently get wrong.

Every fact here is measured rather than assumed, and every one of them is
invisible in the output when it breaks — the run exits 0 and the archives still
exist and still look like backups.
"""
import os
import re
import subprocess
import time
import tarfile
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parent.parent / "agent-mgr"
HOWTO = Path(__file__).resolve().parent.parent / "docs" / "HOWTO.md"
RUN_DIR = re.compile(r"\d{8}T\d{6}Z-\d+")


def run_documented_cron(home, sandbox, extra_path=None):
    """Run the entry as written — schedule stripped, installed path substituted,
    nothing else — with `~` resolving through `home`.

    The running, not just the building, because the line ends in `rm -rf` over a
    `~` this deliberately leaves unexpanded. Handing that string back to a
    caller is a loaded gun; `home` must be inside `sandbox`, this test's own
    `tmp_path`.

    Anchored to that rather than to the system temp root, which is what it
    actually means and what survives `--basetemp` pointed elsewhere. Resolved on
    both sides: on macOS `gettempdir()` is `/var/folders/…` while pytest
    `resolve()`s its basetemp to `/private/var/folders/…`, so comparing the
    unresolved pair refuses every run on the platform this entry exists for —
    measured. A guard that fails closed everywhere protects nothing."""
    assert Path(home).resolve().is_relative_to(Path(sandbox).resolve()), \
        f"refusing to run a documented `rm -rf` with HOME={home}, outside {sandbox}"
    line = next(l for l in HOWTO.read_text().splitlines()
                if l.lstrip().startswith("0 4 * * *") and "find -H" in l)
    # The group and its redirect are the whole point of the entry: without them
    # only the prune's output is logged, and every diagnostic comes from the
    # backup. `documented_prune` also splits on `; }`, so this is what lets that
    # split rest on something enforced rather than on the current phrasing.
    assert re.search(r"\{\s+\S*agent-mgr backup-homes ", line) and "; } >>" in line, \
        f"the entry no longer groups both halves into the log: {line}"
    return subprocess.run(
        ["sh", "-c", line.split(None, 5)[5].replace("~/.local/bin/agent-mgr", str(CLI))],
        capture_output=True, text=True,
        env={"HOME": str(home), "AGENT_MGR_REGISTRY": str(home / "registry"),
             "PATH": f"{extra_path}:{os.environ['PATH']}" if extra_path
                     else os.environ["PATH"]})


def documented_prune(dest):
    """The `find` half of the HOWTO's cron line, retargeted at `dest`. Extracted
    rather than copied: a test carrying its own copy of a destructive command
    pins the copy, which is what lets the real one drift."""
    line = next(l for l in HOWTO.read_text().splitlines()
                if l.lstrip().startswith("0 4 * * *") and "find -H" in l)
    # Taken from the BACKUP half rather than hand-copied, so nothing in this file
    # is a second copy of the doc that could drift from it — and the assertion
    # anchors the WHOLE junction rather than the connector, which costs nothing
    # and fails closed. It covers: both halves naming the same destination (rename
    # one and the cron backs up to A while `rm -rf`-ing B, in either direction),
    # and the `&&` acting on the backup's own exit status. The second is not the
    # same as `&&` being present: `backup-homes <dest> | tee -a log && find …`
    # takes the pipeline's status from `tee`, and `… || true && find …` parses
    # left to right, so both leave the prune ungated while reading fine. Then a
    # run of failed nights prunes the destination empty while writing nothing.
    backup_dest = line.split("backup-homes", 1)[1].split()[0]
    assert f"backup-homes {backup_dest} && find -H {backup_dest} " in line, \
        f"the prune is no longer gated on this backup succeeding: {line}"
    # Up to the `; }` closing the group, so the log redirect after it does not
    # run here — the destination under test is a fixture, not the operator's.
    cmd = line[line.index("find -H"):].split("; }")[0].replace(backup_dest, str(dest))
    # And fail loudly rather than open: `sh` tilde-expands, so a retarget that
    # missed would run `rm -rf` against the operator's REAL backups on this host.
    # The assertions below would fail too, but only after the deletion.
    assert str(dest) in cmd and "~" not in cmd, f"retarget missed the destination: {cmd}"
    return cmd


@pytest.fixture
def dest(tmp_path):
    """`backup-homes` refuses a destination anyone else can write, and the
    runner's umask is not ours to assume — this host's default of 002 makes a
    plain `mkdir` 0775. Pinning the mode here is the same move as forcing
    `umask 022` on the child: it makes each test assert the script's behaviour
    rather than the host's."""
    d = tmp_path / "dest"
    d.mkdir()
    d.chmod(0o700)
    return d


def spawn(home_root, dest, extra_path=None):
    """The launch contract, in one place. `run()` is this plus a wait."""
    # umask 022 forced on the child: it would otherwise inherit the runner's, and
    # on a host already defaulting to 0077 the mode assertion below would pass
    # with `umask 077` deleted from the script — silently ceasing to guard the
    # credential exposure it exists for.
    # Through the CLI, not the library file: `agent-mgr backup-homes` is the only
    # installed entry point, so every assertion below crosses the dispatch arm —
    # a dropped arm, a lost "$@", or a swallowed exit status fails the suite.
    return subprocess.Popen([str(CLI), "backup-homes", str(dest)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            preexec_fn=lambda: os.umask(0o022),
                            env={"HOME": str(home_root),
                                 "AGENT_MGR_REGISTRY": str(home_root / "registry"),
                                 # The suite poisons PATH with its own docker
                                 # stub and refuses any env not carrying it.
                                 "PATH": f"{extra_path}:{os.environ['PATH']}"
                                         if extra_path else os.environ["PATH"]})


def run(home_root, dest, extra_path=None):
    p = spawn(home_root, dest, extra_path)
    out, err = p.communicate()
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


def test_a_symlinked_home_is_archived_and_the_bulk_is_not(tmp_path, dest):
    """A home symlinked onto a bigger disk is supported. Archived from the
    PARENT, tar stores that symlink as a symlink and exits 0 with an archive
    holding one entry and no credentials at all. And logs/ is most of the bytes
    and none of the value."""
    root, target = tmp_path / "home", tmp_path / "big" / "state"
    target.mkdir(parents=True), root.mkdir()
    (root / ".hermes-rowan").symlink_to(target)
    (target / ".env").write_text("PLOW_CHAT_TOKEN=x\n")
    (target / "logs").mkdir()
    (target / "logs" / "bulk").write_text("x" * 4096)

    assert run(root, dest).returncode == 0
    inside = set(tarfile.open(next(dest.glob("*/*.tar.gz"))).getnames())
    assert "./.env" in inside, f"the symlink was stored, not followed: {inside}"
    assert not any("logs" in m for m in inside)


def test_neither_the_run_directory_nor_the_archives_are_readable_by_others(tmp_path, dest):
    """They hold .env and auth.json. Both modes come from `umask 077` and both
    fail open without it — archives land 0644, or 0664 on a host with a laxer
    default, which is what happened the first time these were taken by hand. The
    directory matters as much: it is what stands between a predictable archive
    name and anything else able to write the destination. The forced `umask 022`
    in run() removes the runner's own umask as a second source, so this asserts
    the script's guarantee and not the host's."""
    root = tmp_path / "home"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    assert run(root, dest).returncode == 0
    archive = next(dest.glob("*/*.tar.gz"))
    for path in (archive, archive.parent):
        mode = path.stat().st_mode & 0o777
        assert mode & 0o077 == 0, f"{path.name} is {oct(mode)}"


def test_a_run_that_matches_no_home_fails_instead_of_reporting_success(tmp_path, dest):
    """Under the nightly cron the likely cause is a crontab installed under a
    different account, so $HOME is not the operator's. Exiting 0 there means
    every run reports success having written nothing, discovered at restore."""
    root = tmp_path / "empty-home"
    root.mkdir()

    r = run(root, dest)
    assert r.returncode != 0
    assert "no homes matched" in r.stderr

def test_each_run_gets_its_own_directory(tmp_path, dest):
    """The run directory carries a UTC timestamp *and* the pid, so no two runs
    share one and no archive path is ever reused. Asserting the shape catches
    both regressions deterministically; counting directories only catches the
    date-only one, and then only when the two runs land in the same second."""
    root = tmp_path / "home"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    ps = [spawn(root, dest) for _ in range(2)]
    # communicate(), not wait(): spawn() gives the children pipes, and wait()
    # on an undrained pipe deadlocks as soon as a child fills the buffer —
    # tar writes a "file changed as we read it" line per home.
    outs = [p.communicate() for p in ps]
    assert [p.returncode for p in ps] == [0, 0], outs

    names = sorted(d.name for d in dest.iterdir())
    assert len(names) == 2, f"two runs shared a directory: {names}"
    pat = re.compile(r"\d{8}T\d{6}Z-(\d+)")
    pids = [pat.fullmatch(n).group(1) for n in names if pat.fullmatch(n)]
    assert len(pids) == 2, f"run directories lost their timestamp-pid shape: {names}"
    assert pids[0] != pids[1], "both runs used the same pid field"


def test_a_destination_that_does_not_exist_is_refused_and_not_created(tmp_path):
    """An unmounted NAS or a typo'd path. The run must fail rather than build
    the tree locally, report success, and leave the archives on the wrong disk
    — or gone at the next mount."""
    root, dest = tmp_path / "home", tmp_path / "not" / "mounted"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    r = run(root, dest)
    assert r.returncode != 0, r.stdout
    assert not dest.exists(), "it created the destination instead of failing"
    # Both branches, because they are what the separate message buys: the
    # loose-permissions remedy (`chmod 700`) is useless on a path that is not
    # there, and the mount-point half is the one standing between an operator
    # and a backup written to the local disk under an unmounted mount point.
    assert "does not exist" in r.stderr, r.stderr
    assert "mount it instead" in r.stderr, r.stderr


def test_a_destination_symlinked_onto_another_disk_is_followed(tmp_path, dest):
    """Supported, the same as a symlinked home — the ownership check has to
    resolve the link and judge the target rather than refuse it for not being a
    directory. One case, not a suite-wide parametrization: the empty-sweep and
    two-runs assertions do not become different questions under a symlink."""
    root, target = tmp_path / "home", tmp_path / "big" / "store"
    (root / ".hermes-rowan").mkdir(parents=True), target.mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")
    target.chmod(0o700)
    dest.rmdir(), dest.symlink_to(target)

    assert run(root, dest).returncode == 0
    assert next(target.glob("*/*.tar.gz")).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("mode", [0o775, 0o757], ids=["group-writable", "other-writable"])
def test_a_destination_others_can_write_is_refused(tmp_path, mode):
    """The archives hold `.env` and `auth.json`, and anything able to write the
    destination can redirect one — by planting at a predictable name, or by
    replacing the run directory after `mkdir` made it, since write access to a
    directory is permission to unlink and rename its entries. No sequence of
    syscalls inside the run wins that race, so ownership of the destination is
    the precondition and this is where it is enforced."""
    root, dest = tmp_path / "home", tmp_path / "loose-dest"
    (root / ".hermes-rowan").mkdir(parents=True), dest.mkdir()
    (root / ".hermes-rowan" / ".env").write_text("x\n")
    dest.chmod(mode)  # not mkdir(mode=), which the umask masks

    r = run(root, dest)
    assert r.returncode != 0, r.stdout
    assert "nobody else can write" in r.stderr
    assert not any(dest.iterdir()), "it wrote archives anyway"


def test_the_documented_prune_takes_only_what_this_command_wrote(tmp_path, dest):
    """The highest-consequence snippet in these docs — an `rm -rf` the operator
    pastes into cron — and it has shipped over-broad twice: once taking every
    directory, once every `*.tar.gz`. The run directory here is the real one the
    script just wrote, not a hand-copied name, so the glob is checked against
    what `lib/backup-homes` actually produces. Change either and this fails,
    rather than the nightly quietly pruning nothing while reporting success.

    One entry per clause: the two name globs keep the operator's own `photos/`
    and `photos-2019.tar.gz`, each `-type` keeps the same-named impostor of the
    wrong kind, `-maxdepth` keeps a matching name *nested* inside a directory of
    theirs, and `-mtime` keeps a run taken since. Every one of those is named to
    match some other clause's predicate, so it tests the clause it exists for."""
    root = tmp_path / "home"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")
    assert run(root, dest).returncode == 0
    aged_run = next(dest.iterdir())

    (dest / "hermes-errands-20260101.tar.gz").write_text("what an earlier shape wrote\n")
    (dest / "photos-2019.tar.gz").write_text("the operator's own\n")
    (dest / "photos").mkdir()
    # Only `-maxdepth 1` protects this: it matches the flat arm's glob exactly,
    # and a top-level name glob does nothing for a nested file.
    nested = dest / "photos" / "hermes-2019.tar.gz"
    nested.write_text("inside the operator's own directory\n")
    # The two impostors, one per `-type`. Named to MATCH the other arm's glob,
    # which is the whole point: a trap the name globs already exclude tests
    # nothing about `-type`.
    (dest / "hermes-trap.tar.gz").mkdir()          # a directory named like an archive
    (dest / "20260101T000000Z-9").write_text("x\n")  # a file named like a run

    old = time.time() - 20 * 86400
    for p in dest.iterdir():
        os.utime(p, (old, old))
    os.utime(nested, (old, old))  # the loop above walks top level only

    # A second run AFTER the ageing, so the fixture holds something recent. With
    # everything aged, the survivor set is identical whether the line says
    # `-mtime +14`, `-mtime +7`, or carries no `-mtime` at all — and dropping it
    # is the mutation where the cron's second half deletes the backup its own
    # first half wrote seconds earlier, forever, while reporting success.
    assert run(root, dest).returncode == 0
    fresh_run = next(p for p in dest.iterdir()
                     if p != aged_run and p.is_dir() and RUN_DIR.fullmatch(p.name))

    # Pruned through a SYMLINK, the shape the section two paragraphs up invites.
    # Without `-H`, `find` does not descend the starting point, `-mindepth 1`
    # matches nothing, and retention silently stops while the nightly stays green.
    link = tmp_path / "link"
    link.symlink_to(dest)
    subprocess.run(["sh", "-c", documented_prune(link)], check=True)

    assert not aged_run.exists(), "it kept the run directory it was meant to prune"
    assert fresh_run.exists(), "-mtime is not bounding the window: it took a fresh run"
    assert nested.exists(), "-maxdepth is not bounding it: it descended into photos/"
    assert {p.name for p in dest.iterdir()} == {
        "20260101T000000Z-9", "hermes-trap.tar.gz", "photos", "photos-2019.tar.gz",
        fresh_run.name}


def tar_shim(directory, message, only_for=None):
    """A `tar` that reports `message` on stderr and exits 1.

    A shim rather than a real failure, for two reasons: an earlier version of
    this suite raced a live writer and went green wherever the overlap missed,
    and the obvious injection — a mode-000 file — stops injecting under root,
    which is this repo's own worst bug class.

    `only_for` narrows it to homes whose path ends in that string; every other
    home gets a real (empty) archive and success. Without it, every home fails.
    """
    directory.mkdir(exist_ok=True)
    guard = f'case "$home" in *{only_for}) ;; *) : > "$out"; exit 0;; esac\n' if only_for else ""
    (directory / "tar").write_text(
        "#!/bin/sh\nprev=; out=; home=\nfor a in \"$@\"; do\n"
        '  [ "$prev" = -czf ] && out="$a"\n  [ "$prev" = -C ] && home="$a"\n  prev="$a"\ndone\n'
        + guard
        + '[ -n "$out" ] && : > "$out"\n'
        + f'[ -n "{message}" ] && printf \'%s\\n\' "{message}" >&2\nexit 1\n')
    (directory / "tar").chmod(0o755)
    return str(directory)


@pytest.mark.parametrize("message,tolerated", [
    ("tar: ./sessions.db: file changed as we read it", True),
    ("tar: ./kanban.db-wal: File removed before we read it", True),
    ("tar: ./app.log: File shrank by 4096 bytes; padding with zeros", True),
    ("tar: ./sessions.db: file changed as we read it\ntar: ./app.log: File shrank by 8 bytes; padding with zeros", True),
    ("tar: Can't open 'auth.json': Permission denied", False),
    ("tar: ./x: Cannot stat: No such file or directory", False),
    ("", False),
], ids=["read-race", "removed-mid-read", "shrank", "race-and-shrank",
        "unreadable-member", "unstattable", "no-diagnostic-at-all"])
def test_tar_status_1_is_judged_by_its_message_not_its_number(
        tmp_path, dest, message, tolerated):
    """`tar` exits 1 for all four and they are opposite outcomes: the first two
    leave an archive missing something that was being rewritten anyway, the last
    two leave one missing a file that was simply never read — `auth.json` being
    the case this command exists for.

    The number cannot separate them across the two tars this repo supports.
    bsdtar keeps 1 for a member it could not open, which it then omits — so a
    status-only tolerance publishes a credential-less archive, exits 0, and lets
    retention prune the good copies. The measured race shapes live in one place,
    the `benign` comment in `lib/backup-homes`; this does not restate them."""
    root = tmp_path / "home"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")

    r = run(root, dest, extra_path=tar_shim(tmp_path / "bin", message))
    assert (r.returncode == 0) is tolerated, f"exit {r.returncode}: {r.stderr}"
    if message:
        assert message.splitlines()[0] in r.stderr, "tar's diagnostic must reach the operator"
    archives = list(dest.glob("*/*.tar.gz"))
    if tolerated:
        assert archives, "a tolerated race must still leave its archive"
    else:
        assert "tar failed" in r.stderr
        # Complete, valid, `gzip -t`-clean and missing a file is worse than
        # absent: a restore reaches for it as the newest thing there.
        assert not archives, "it kept an archive tar could not finish"


def test_one_failing_home_does_not_cost_the_others_their_night(tmp_path, dest):
    """The abort set now includes any status-1 message nobody has met yet, so an
    `exit` here would turn a run that could archive four homes into one that
    archives none — and the cron's `&&` would then leave the operator with no
    fresh copy of anything. The run must still fail, loudly, at the end."""
    root = tmp_path / "home"
    for name in (".hermes-alpha", ".hermes-omega"):
        (root / name).mkdir(parents=True)
        (root / name / ".env").write_text("x\n")

    shim = tar_shim(tmp_path / "bin", "tar: Cannot open: Permission denied",
                    only_for="alpha")

    r = run(root, dest, extra_path=shim)
    assert r.returncode != 0, "a home that could not be archived must fail the run"
    assert [p.name for p in dest.glob("*/*.tar.gz")] == ["hermes-omega.tar.gz"], \
        "the healthy home lost its night to the broken one"


@pytest.mark.parametrize("case", ["a home fails", "the destination is missing"])
def test_the_documented_cron_entry_leaves_a_trace_when_the_night_fails(tmp_path, case):
    """The night worth hearing about is the one that failed, and cron has no
    `MAILTO` here — on a host without a working MTA its output is discarded. So
    the entry groups both halves and redirects them, and this runs the entry as
    written rather than a paraphrase.

    Both branches, because they fail in different places: a home that cannot be
    archived is the script talking, while a missing destination is the shell
    failing to even reach it — the unmounted-disk night, and the reason the log
    lives outside the destination rather than in it."""
    root = tmp_path / "home"
    for name in (".hermes-alpha", ".hermes-omega"):
        (root / name).mkdir(parents=True)
        (root / name / ".env").write_text("x\n")

    shim = None
    if case == "a home fails":
        (root / "agent-backups").mkdir()
        (root / "agent-backups").chmod(0o700)
        shim = tar_shim(tmp_path / "bin", "tar: Cannot open: Permission denied",
                        only_for="alpha")

    r = run_documented_cron(root, tmp_path, extra_path=shim)

    assert r.returncode != 0, "a failed night must hold the prune back"
    log = (root / "backup-homes.log").read_text()
    if case == "a home fails":
        assert "tar failed on" in log, f"the backup half never reached the log: {log!r}"
        assert "one or more homes were not archived" in log, log
        assert [p.name for p in (root / "agent-backups").glob("*/*.tar.gz")] == \
            ["hermes-omega.tar.gz"], "the healthy home lost its night to the broken one"
    else:
        assert "does not exist" in log, \
            f"the unmounted-destination night left no trace: {log!r}"
        assert not (root / "agent-backups").exists(), "it created the destination"


