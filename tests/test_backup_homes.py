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


def documented_prune(dest):
    """The `find` half of the HOWTO's cron line, retargeted at `dest`. Extracted
    rather than copied: a test carrying its own copy of a destructive command
    pins the copy, which is what lets the real one drift."""
    line = next(l for l in HOWTO.read_text().splitlines()
                if l.lstrip().startswith("0 4 * * *") and "find -H" in l)
    # The `&&` is load-bearing and cannot survive the slice below, so assert it
    # here: it gates the prune on the backup having SUCCEEDED. Split the cron
    # entry in two, or swap it for `;`, and a run of failed nights prunes the
    # destination empty while writing nothing.
    assert "&& find -H" in line, f"the prune is no longer gated on the backup: {line}"
    cmd = line[line.index("find -H"):].replace("~/agent-backups", str(dest))
    # Fail loudly rather than open. `~/agent-backups` is the one part still
    # hand-copied here, so an ordinary docs rename makes `replace` match nothing
    # — and `sh` tilde-expands, so the command would run `rm -rf` against the
    # operator's REAL backups on the host this suite runs on. The assertions
    # below would then fail, but only after the deletion.
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


def spawn(home_root, dest):
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
                                 "PATH": os.environ["PATH"]})


def run(home_root, dest):
    p = spawn(home_root, dest)
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
