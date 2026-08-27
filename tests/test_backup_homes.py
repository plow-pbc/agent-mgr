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

    Two floors, because each misses what the other catches, and neither may
    depend on where the temp root is — an earlier pair did, and refused every
    run under `--basetemp` and on macOS, where `gettempdir()` is
    `/var/folders/…` while pytest `resolve()`s its basetemp to
    `/private/var/folders/…`. Measured.

    So: strict containment in this test's sandbox, which says what it means, and
    `home` is not the real home, which is the catastrophic case stated directly
    and is the one no combination of arguments can satisfy. A guard that fails
    closed everywhere protects nothing; one its own argument satisfies protects
    nothing either."""
    home, sandbox = Path(home).resolve(), Path(sandbox).resolve()
    assert home.is_relative_to(sandbox) and home != sandbox, \
        f"refusing to run a documented `rm -rf` with HOME={home}, outside {sandbox}"
    assert home != Path.home().resolve(), \
        f"refusing to run a documented `rm -rf` against the real home {home}"
    line = next(l for l in HOWTO.read_text().splitlines()
                if l.lstrip().startswith("0 4 * * *") and "agent-mgr backup-homes" in l)
    # The group and its redirect are the whole point of the entry: without them
    # only the prune's output is logged, and every diagnostic comes from the
    # backup. The `&&` is the other half — retention must run only after the
    # backup it prunes for succeeded — and both halves must name one destination.
    assert re.search(
        r"\{\s+\S*agent-mgr backup-homes (\S+) && \S*agent-mgr prune-backups \1 ", line) \
        and "; } >>" in line, \
        f"the entry no longer groups a gated backup-and-prune into the log: {line}"
    return subprocess.run(
        ["sh", "-c", line.split(None, 5)[5].replace("~/.local/bin/agent-mgr", str(CLI))],
        capture_output=True, text=True,
        env={"HOME": str(home), "AGENT_MGR_REGISTRY": str(home / "registry"),
             "PATH": f"{extra_path}:{os.environ['PATH']}" if extra_path
                     else os.environ["PATH"]})


@pytest.fixture
def home(tmp_path):
    """One registered agent with a credential in it — the arrangement almost
    every test below needs and none of them varies."""
    root = tmp_path / "home"
    (root / ".hermes-rowan").mkdir(parents=True)
    (root / ".hermes-rowan" / ".env").write_text("x\n")
    return root


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
    inside = set(tarfile.open(next(dest.glob("backup-homes/*/*.tar.gz"))).getnames())
    assert "./.env" in inside, f"the symlink was stored, not followed: {inside}"
    assert not any("logs" in m for m in inside)


def test_neither_the_run_directory_nor_the_archives_are_readable_by_others(home, dest):
    """They hold .env and auth.json. All three modes come from `umask 077` and
    fail open without it — archives land 0644, or 0664 on a host with a laxer
    default, which is what happened the first time these were taken by hand. The
    directory matters as much: it is what stands between a predictable archive
    name and anything else able to write the destination. The forced `umask 022`
    in run() removes the runner's own umask as a second source, so this asserts
    the script's guarantee and not the host's."""

    # Pre-created loose, which is the documented migration: the operator makes
    # `backup-homes/` by hand to move older runs into it, and a default umask of
    # 022 gives them 0755. Nothing else pins the `chmod` that fixes it — under
    # the suite's own umask the directory would be 0700 either way.
    (dest / "backup-homes").mkdir()
    (dest / "backup-homes").chmod(0o755)

    assert run(home, dest).returncode == 0
    archive = next(dest.glob("backup-homes/*/*.tar.gz"))
    for path in (archive, archive.parent, archive.parent.parent):
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

def test_each_run_gets_its_own_directory(home, dest):
    """The run directory carries a UTC timestamp *and* the pid, so no two runs
    share one and no archive path is ever reused. Asserting the shape catches
    both regressions deterministically; counting directories only catches the
    date-only one, and then only when the two runs land in the same second."""

    ps = [spawn(home, dest) for _ in range(2)]
    # communicate(), not wait(): spawn() gives the children pipes, and wait()
    # on an undrained pipe deadlocks as soon as a child fills the buffer —
    # tar writes a "file changed as we read it" line per home.
    outs = [p.communicate() for p in ps]
    assert [p.returncode for p in ps] == [0, 0], outs

    names = sorted(d.name for d in (dest / "backup-homes").iterdir())
    assert len(names) == 2, f"two runs shared a directory: {names}"
    pat = re.compile(r"\d{8}T\d{6}Z-(\d+)")
    pids = [pat.fullmatch(n).group(1) for n in names if pat.fullmatch(n)]
    assert len(pids) == 2, f"run directories lost their timestamp-pid shape: {names}"
    assert pids[0] != pids[1], "both runs used the same pid field"


def test_a_destination_that_does_not_exist_is_refused_and_not_created(tmp_path, home):
    """An unmounted NAS or a typo'd path. The run must fail rather than build
    the tree locally, report success, and leave the archives on the wrong disk
    — or gone at the next mount."""
    dest = tmp_path / "not" / "mounted"

    r = run(home, dest)
    assert r.returncode != 0, r.stdout
    assert not dest.exists(), "it created the destination instead of failing"
    # Both branches, because they are what the separate message buys: the
    # loose-permissions remedy (`chmod 700`) is useless on a path that is not
    # there, and the mount-point half is the one standing between an operator
    # and a backup written to the local disk under an unmounted mount point.
    assert "does not exist" in r.stderr, r.stderr
    assert "mount it instead" in r.stderr, r.stderr


def test_a_destination_symlinked_onto_another_disk_is_followed(tmp_path, home, dest):
    """Supported, the same as a symlinked home — the ownership check has to
    resolve the link and judge the target rather than refuse it for not being a
    directory. One case, not a suite-wide parametrization: the empty-sweep and
    two-runs assertions do not become different questions under a symlink."""
    target = tmp_path / "big" / "store"
    target.mkdir(parents=True)
    target.chmod(0o700)
    dest.rmdir(), dest.symlink_to(target)

    assert run(home, dest).returncode == 0
    assert next(target.glob("backup-homes/*/*.tar.gz")).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("mode", [0o775, 0o757], ids=["group-writable", "other-writable"])
def test_a_destination_others_can_write_is_refused(tmp_path, home, mode):
    """The archives hold `.env` and `auth.json`, and anything able to write the
    destination can redirect one — by planting at a predictable name, or by
    replacing the run directory after `mkdir` made it, since write access to a
    directory is permission to unlink and rename its entries. No sequence of
    syscalls inside the run wins that race, so ownership of the destination is
    the precondition and this is where it is enforced."""
    dest = tmp_path / "loose-dest"
    dest.mkdir()
    dest.chmod(mode)  # not mkdir(mode=), which the umask masks

    r = run(home, dest)
    assert r.returncode != 0, r.stdout
    assert "nobody else can write" in r.stderr
    assert not any(dest.iterdir()), "it wrote archives anyway"


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
    ("tar: ./auth.json: File removed before we read it", False),
    ("tar: ./sessions.db-journal: File removed before we read it", True),
    ("tar: ./app.log: File shrank by 4096 bytes; padding with zeros", True),
    ("tar: ./sessions.db: file changed as we read it\ntar: ./app.log: File shrank by 8 bytes; padding with zeros", True),
    ("tar: Can't open 'auth.json': Permission denied", False),
    ("tar: ./x: Cannot stat: No such file or directory", False),
    ("", False),
], ids=["read-race", "wal-removed", "credential-removed", "journal-removed", "shrank",
        "race-and-shrank", "unreadable-member", "unstattable",
        "no-diagnostic-at-all"])
def test_tar_status_1_is_judged_by_its_message_not_its_number(
        tmp_path, home, dest, message, tolerated):
    """`tar` exits 1 for all four and they are opposite outcomes: the first two
    leave an archive missing something that was being rewritten anyway, the last
    two leave one missing a file that was simply never read — `auth.json` being
    the case this command exists for. A *removal* splits the same way on its
    path rather than its wording: the gateway checkpoints its SQLite sidecars
    away mid-run every night, while `auth.json` vanishing means an atomic
    rewrite caught mid-flight, and publishing without it is the same loss.

    The number cannot separate them across the two tars this repo supports.
    bsdtar keeps 1 for a member it could not open, which it then omits — so a
    status-only tolerance publishes a credential-less archive, exits 0, and lets
    retention prune the good copies. The measured race shapes live in one place,
    the `benign` comment in `lib/backup-homes`; this does not restate them."""

    r = run(home, dest, extra_path=tar_shim(tmp_path / "bin", message))
    assert (r.returncode == 0) is tolerated, f"exit {r.returncode}: {r.stderr}"
    if message:
        assert message.splitlines()[0] in r.stderr, "tar's diagnostic must reach the operator"
    archives = list(dest.glob("backup-homes/*/*.tar.gz"))
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
    assert [p.name for p in dest.glob("backup-homes/*/*.tar.gz")] == ["hermes-omega.tar.gz"], \
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
        assert [p.name for p in (root / "agent-backups").glob("backup-homes/*/*.tar.gz")] == \
            ["hermes-omega.tar.gz"], "the healthy home lost its night to the broken one"
    else:
        assert "does not exist" in log, \
            f"the unmounted-destination night left no trace: {log!r}"
        assert not (root / "agent-backups").exists(), "it created the destination"


def test_prune_takes_runs_and_nothing_beside_them(tmp_path, home, dest):
    """`prune-backups` is an `rm -rf` over a directory the operator chose and
    keeps their own things in. Three basename grammars were written to tell the
    two apart and each still deleted something of theirs; runs live in a private
    child now, so what protects the operator is structure rather than a pattern.

    The aged run is the real name the script writes, not a hand-copied one."""
    assert run(home, dest).returncode == 0
    runs = dest / "backup-homes"
    aged_run = next(runs.iterdir())

    mine = {"photos": "dir", "photos-2019.tar.gz": "file",
            "20240101T010101Z-1": "dir"}  # even run-shaped, it is outside the child
    for name, kind in mine.items():
        (dest / name).mkdir() if kind == "dir" else (dest / name).write_text("x\n")
    stray = runs / "note-to-self.txt"   # not a run; -type d leaves it alone
    stray.write_text("why is this here\n")
    boundary = runs / "20240101T010101Z-2"  # exactly at the window's edge
    boundary.mkdir()

    old, edge = time.time() - 20 * 86400, time.time() - 14 * 86400 - 60
    for p in [*dest.iterdir(), *runs.iterdir()]:
        os.utime(p, (old, old))
    os.utime(boundary, (edge, edge))
    # After the ageing, so something recent exists: with everything aged the
    # survivor set is identical whether or not the command bounds a window.
    known = {p.name for p in runs.iterdir()}
    assert run(home, dest).returncode == 0
    # By difference, not by "not the aged one": `runs/` also holds the stray and
    # the boundary run by now, so `next()` would bind to whichever `scandir`
    # happened to yield — passing vacuously on the stray, or failing on the
    # boundary, which is *supposed* to be deleted.
    fresh_run = next(p for p in runs.iterdir() if p.name not in known)

    link = tmp_path / "link"           # the symlinked destination the docs invite
    link.symlink_to(dest)
    assert subprocess.run([str(CLI), "prune-backups", str(link), "14"],
                          capture_output=True, text=True).returncode == 0

    assert not aged_run.exists(), "it kept the run it was meant to prune"
    assert fresh_run.exists(), "it took a run inside the retention window"
    assert {p.name for p in dest.iterdir()} == set(mine) | {"backup-homes"}, \
        "it reached outside its own child"
    assert stray.exists(), "it deleted a file that is not a run"
    # `! -mtime -14` takes a run at exactly fourteen days; `-mtime +14` would
    # keep it, which is a fifteenth daily archive under a documented fourteen.
    assert not boundary.exists(), "it kept a run at the retention boundary"


def test_a_symlinked_runs_child_is_refused_by_both_halves(tmp_path, home, dest):
    """Supporting it would reopen the class the child exists to close: a link
    the operator pointed at a disk that already holds their things puts those
    things *inside* the namespace, where retention deletes any directory older
    than the window. `mkdir -p` and `chmod` both succeed quietly on a link, so
    the backup half would adopt and re-mode the target too.

    Refusing costs nothing — the *destination* may be a symlink, which is how
    runs reach a bigger disk in the first place."""
    target = tmp_path / "elsewhere"
    (target / "photos").mkdir(parents=True)
    old = time.time() - 20 * 86400
    os.utime(target / "photos", (old, old))
    (dest / "backup-homes").symlink_to(target)

    r = run(home, dest)
    assert r.returncode != 0 and "is a symlink" in r.stderr, r.stderr
    p = subprocess.run([str(CLI), "prune-backups", str(dest), "14"],
                       capture_output=True, text=True)
    assert p.returncode != 0 and "is a symlink" in p.stderr, p.stderr
    assert (target / "photos").exists(), "it deleted through the link anyway"


@pytest.mark.parametrize("days", ["-1", "0", "00", "14 -o -true", "abc", "1.5"],
                         ids=["negative", "zero", "padded-zero", "injected",
                              "word", "fraction"])
def test_prune_refuses_a_retention_argument_it_cannot_trust(tmp_path, home, dest, days):
    """`days` lands inside `find`'s own expression. `-1` becomes `-mtime +-1`,
    which GNU findutils 4.9 matches against a *fresh* directory — measured — so
    one typo'd argument sends the whole backup set to `rm -rf`."""
    assert run(home, dest).returncode == 0
    before = {p.name for p in (dest / "backup-homes").iterdir()}
    assert before, "fixture sanity: there is a run to lose"

    r = subprocess.run([str(CLI), "prune-backups", str(dest), days],
                       capture_output=True, text=True)
    assert r.returncode != 0, r.stdout
    assert "days must be" in r.stderr, r.stderr
    assert {p.name for p in (dest / "backup-homes").iterdir()} == before


def test_prune_refuses_a_destination_it_has_never_written_to(tmp_path):
    """An unmounted mount point or a typo'd path. Deleting nothing quietly is
    the wrong answer: the caller is the cron's `&&`, and a silent success there
    reads as a completed retention. The message names the mount case because
    that is the one where the directory's absence is a symptom, not the fact."""
    r = subprocess.run([str(CLI), "prune-backups", str(tmp_path / "nope")],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "no runs" in r.stderr and "not mounted" in r.stderr, r.stderr
