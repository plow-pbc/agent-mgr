import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The PATH the suite inherited, before it was made docker-free. Only
# tests/test_compose.py uses it -- see the fixture below.
REAL_PATH = os.environ.get("PATH", "")

# Pytest's tmp root: every docker this suite is allowed to run -- the session
# stub and any fake_docker -- is written under it. Set by the fixture below.
SUITE_TMP = None


# The default `docker` every test gets: answers the two READ calls agent-mgr
# makes, and refuses everything else.
#
# It needs no arguments because agent-mgr exports AGENT_PROJECT, AGENT_CONTAINER
# and AGENT_HOME before shelling out, so the config it renders is self-consistent
# with whatever agent is being resolved -- resolve-guard passes for any name.
# `ps` reports no running gateway, so nothing reaches a restart.
#
# Refusing every other subcommand is the load-bearing half: a test must not be
# able to start, stop or restart a container even by accident, because the
# project it would name is production's.
SAFE_DOCKER = """#!/usr/bin/env bash
case "$*" in
  *"config --format json"*)
    cat <<JSON
{"name": "${AGENT_PROJECT:-unset}",
 "services": {"hermes": {"container_name": "${AGENT_CONTAINER:-unset}",
   "image": "${AGENT_IMAGE:-nousresearch/hermes-agent@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc}",
   "volumes": [{"target": "/opt/data", "source": "${AGENT_HOME:-unset}"}]}}}
JSON
    ;;
  *"ps -a --quiet"*) ;;
  *"ps --status running --quiet"*) ;;
  *)
    echo "refusing a docker call a test did not stub: docker $*" >&2
    exit 97 ;;
esac
exit 0
"""


_ALLOW_REAL_DOCKER = False


@contextlib.contextmanager
def allow_real_docker():
    """The one deliberate exemption: tests/test_compose.py renders the real
    template with the real `compose config`, which never contacts the daemon."""
    global _ALLOW_REAL_DOCKER
    _ALLOW_REAL_DOCKER = True
    try:
        yield
    finally:
        _ALLOW_REAL_DOCKER = False


def _docker_the_suite_owns(path):
    """Which docker would this PATH find, and did the suite create it?

    Asked as a positive property so it subsumes every way of reaching the real
    binary -- a PATH built from scratch, one that merely puts the real bindir
    ahead of the shadow, a second docker somewhere else entirely -- without
    enumerating them, and without assuming where the operator's docker lives.
    """
    found = shutil.which("docker", path=path)
    return bool(found) and pathlib.Path(found).is_relative_to(SUITE_TMP)


def spawn(argv, env, **kw):
    """Convenience wrapper: the suite's usual capture_output/text defaults."""
    return subprocess.run(argv, capture_output=True, text=True, env=env, **kw)


@pytest.fixture(scope="session", autouse=True)
def _no_real_docker_on_path(tmp_path_factory):
    """Take the real `docker` off PATH for the whole suite.

    This suite was hermetic in every dimension it thought to isolate and not in
    the one that mattered: AGENT_PROJECT defaults to `hermes-<name>`, so a
    fixture agent called `rowan` or `str` resolves to the LIVE compose project.
    A test that reached the real daemon therefore did not fail -- it restarted
    production. One run issued 20 `compose restart hermes` calls against live
    projects; a day of PR iteration came to 917 boots of the rentals gateway,
    1,378 of rowan's, and 207 shutdown notices into an owners' channel
    (plow-pbc/agent-mgr#13).

    Poisoning the process PATH rather than each fixture's is what closes the
    class. Tests routinely build their own PATH as f"{mybin}:{os.environ['PATH']}",
    which silently re-admitted the real binary however carefully the fixture
    below prepended a fake -- and that shape is how the live restarts survived
    the first attempt at this fix.
    """
    global SUITE_TMP
    b = tmp_path_factory.mktemp("poison-bin")
    (b / "docker").write_text(SAFE_DOCKER)
    (b / "docker").chmod(0o755)
    # Prepended, not filtered: `docker` lives in /usr/bin beside python3, bash
    # and every other tool the suite shells out to, so removing the directory
    # removes the suite. Shadowing is enough -- a test building
    # f"{mybin}:{os.environ['PATH']}" still puts this ahead of /usr/bin.
    os.environ["PATH"] = os.pathsep.join([str(b), REAL_PATH])
    SUITE_TMP = tmp_path_factory.getbasetemp()

    # Enforced on subprocess itself, not offered as a helper. A seam callers
    # must remember to use is the convention this change exists to retire, and
    # the violation that actually restarted production was a bare
    # subprocess.run the `run` fixture never saw.
    #
    # Popen rather than run: run, call, check_call and check_output all funnel
    # through it, so one wrapper covers every entry point -- including a module
    # that bound `from subprocess import run` at import time, which collection
    # has already done by the time this fixture is set up.
    real_popen = subprocess.Popen

    def guarded_popen(*a, **kw):
        env = kw.get("env")
        # `is not None`, not `and "PATH" in env`: an env with no PATH key is not
        # inert. The child starts with PATH unset and falls back to the shell's
        # own default (/usr/local/bin:/usr/bin:...), finding the operator's
        # docker there. And it can never be shown to be suite-owned, since
        # shutil.which on the empty string returns None -- so it is refused
        # rather than exempted.
        if env is not None and not _ALLOW_REAL_DOCKER:
            path = env.get("PATH")
            if path is None:
                raise AssertionError(
                    "this env carries no PATH, so the child would fall back to "
                    "the shell's own default and find the operator's docker. "
                    "Pass os.environ['PATH'] to inherit the suite's stub."
                )
            assert _docker_the_suite_owns(path), (
                f"this env resolves docker to {shutil.which('docker', path=path)}, "
                "which the suite did not create; build PATH as "
                "f\"{mybin}:{os.environ['PATH']}\" so the stub still wins, or "
                "use conftest.allow_real_docker()"
            )
        return real_popen(*a, **kw)

    subprocess.Popen = guarded_popen
    try:
        yield
    finally:
        subprocess.Popen = real_popen


# The latch declaration an agent's config carries, in one place: check-latch
# reads it to decide whether an agent has a Mac at all, and set-latch refuses
# without it, so two test modules need the same contract and had a copy each.
LATCH_CONFIG = (
    "model:\n  provider: openai-codex\n"
    "mcp_servers:\n  latch:\n"
    "    url: https://api.plow.co/v1/relay/devices/${DOMO_DEVICE_UID}/mcp\n"
)


@pytest.fixture
def registry(tmp_path):
    """An isolated registry file; never the operator's real one."""
    return tmp_path / "config" / "agent-mgr" / "agents"


@pytest.fixture
def run(registry, tmp_path):
    """Invoke the real agent-mgr CLI with an isolated registry and HOME."""

    def _run(*args, env=None, check=False, input=None):
        e = dict(os.environ)
        e["AGENT_MGR_REGISTRY"] = str(registry)
        e["HOME"] = str(tmp_path / "home")
        (tmp_path / "home").mkdir(exist_ok=True)
        # restore installs the plugin through the same fetch-tree the skills
        # use, and activate curls the activation script from an earlier SHA of
        # that same repo -- so both a hermetic `gh` and a hermetic `curl` are on
        # PATH for every invocation unless a test overrides PATH deliberately.
        b = fake_curl(tmp_path)
        install_fake_gh(tmp_path, b)
        e["PATH"] = f"{b}:{e['PATH']}"
        if env:
            e.update(env)
        # `input` for the commands that read a credential on stdin rather than
        # from argv -- set-latch is the first.
        return spawn([str(ROOT / "agent-mgr"), *args], e, check=check, input=input)

    return _run


@pytest.fixture
def instance(tmp_path):
    """Create an agent's repo on disk and return its path.

    Named `instance` before the repo settled on *agent repo*. The concept it makes is still
    right -- one agent's repo -- so the rename is tracked rather than swept: a
    blind one would also hit `instance override`, which is Compose's word.
    """

    def _instance(name, descriptor="", config="model:\n  provider: openai-codex\n"):
        repo = tmp_path / f"{name}-repo"
        repo.mkdir(exist_ok=True)
        (repo / "agent.env").write_text(descriptor)
        if config is not None:
            (repo / "config.yaml").write_text(config)
        return repo

    return _instance


def fake_docker(
    tmp_path,
    *,
    home,
    container="hermes-<name>",
    project="hermes-<name>",
    name="rowan",
    running=True,
    exec_output=None,
    log=None,
    mount=None,
    exists=None,
    all_cids=(),
    mounts=None,
    image=None,
    build=False,
    pull_policy=None,
):
    """A `docker` that answers the three things agent-mgr asks of it.

    One builder rather than one per test file: every command now passes through
    resolve-guard, so every fake needs a parseable `config --format json` -- and
    three near-copies of that JSON drift the moment the guard reads a new field.

    `log` records argv when given, so a test can assert on what actually ran
    rather than on what the source says.
    """
    import json

    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    container = container.replace("<name>", name)
    project = project.replace("<name>", name)
    svc = {
        "container_name": container,
        "volumes": [{"target": "/opt/data", "source": str(home)}],
    }
    # The image Compose would resolve. A digest by default, because that is what
    # the fleet pins; `image=` or `build=True` let a test say otherwise.
    if build:
        svc["build"] = {"context": "."}
        svc["image"] = image or f"hermes-{name}:local"
    else:
        svc["image"] = image or "nousresearch/hermes-agent@sha256:" + "c" * 64
    if pull_policy:
        svc["pull_policy"] = pull_policy
    cfg = json.dumps({"name": project, "services": {"hermes": svc}})
    parts = [
        "#!/usr/bin/env bash",
        f'case "$*" in *inspect*) echo "{mount}"; exit 0 ;; esac' if mount is not None else "",
        f'printf "%s\\n" "$*" >> {log}' if log else "",
        # And one word per line beside it: the joined form cannot tell an intact
        # argv word from one the caller re-split, which is what the sh -c escape
        # needs observed. Separate file so substring assertions on the joined
        # log keep working.
        f'printf "%s\\n" "$@" >> {log}.argv' if log else "",
        # stdin beside argv, in its OWN file: a test asserting a secret is
        # absent from argv proves nothing about whether it still reaches the
        # command, and one file could not tell the two apart.
        #
        # Gated on `exec -T` because that is what the real command needs to
        # forward a pipe -- without it docker allocates a TTY and refuses piped
        # stdin, so a fake that read the pipe anyway would stay green while the
        # live probe broke. And gated on fd 0 not being a terminal: most execs
        # here inherit the parent's stdin, which under `pytest -s` IS the
        # terminal, and an unconditional `cat` would hang the suite under the
        # ordinary way to debug these tests.
        f'case "$*" in *"exec -T"*) [ -t 0 ] || cat >> {log}.stdin ;; esac' if log else "",
        'case "$*" in',
        f"  *\"config --format json\"*) cat <<'JSON'\n{cfg}\nJSON\n    ;;",
        # `ps -a` answers about EXISTENCE, `--status running` about running.
        # They differ for a stopped container, which is the case the identity
        # seam was blind to, so a test can now set them independently.
        f'  *"ps -a --quiet"*) {"printf '%s\\n' " + " ".join(all_cids) if all_cids else ("echo deadbeef" if (running if exists is None else exists) else ":")} ;;',
        f'  *"ps --status running --quiet"*) {"echo deadbeef" if running else ":"} ;;',
        (
            f'  *inspect*) case "$*" in '
            + " ".join(f"*{c}*) echo {m} ;;" for c, m in (mounts or {}).items())
            + f" *) echo {home} ;; esac ;;"
        )
        if mounts
        else f"  *inspect*) echo {home} ;;",
    ]
    if exec_output is not None:
        parts.append(f"  *exec*) echo {exec_output} ;;")
    parts += ["esac", "exit 0", ""]
    (b / "docker").write_text("\n".join(x for x in parts if x))
    (b / "docker").chmod(0o755)
    return b


def shlex_quote(s):
    import shlex

    return shlex.quote(s)


def fake_curl(tmp_path, *, body="#!/usr/bin/env bash\nexit 0\n", fail=False):
    """A `curl -o <path>` that writes a no-op plugin installer.

    restore installs the plugin, so the real path curls upstream. Stubbing curl
    keeps the suite hermetic while still exercising agent-mgr's own fetch,
    ref-validation and `bash <installer>` steps.
    """
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    script = "#!/usr/bin/env bash\n"
    if fail:
        script += "exit 22\n"
    else:
        script += (
            'out=""\n'
            'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; *) shift ;; esac; done\n'
            f'[ -n "$out" ] && printf %s {shlex_quote(body)} > "$out"\n'
            "exit 0\n"
        )
    (b / "curl").write_text(script)
    (b / "curl").chmod(0o755)
    return b


class CredentialAPI:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object | None, str | None]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else None
                owner.requests.append(
                    (self.command, self.path, body, self.headers.get("Authorization"))
                )
                payload: object = (
                    {"participants": [{"type": "agent", "line": {"uid": "ln_elm"}}]}
                    if self.command == "GET"
                    else {"chat_uids": ["line:ln_elm"]}
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            do_GET = _handle
            do_PUT = _handle

            def log_message(self, format: str, *args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def credential_api() -> Iterator[CredentialAPI]:
    server = CredentialAPI()
    server.thread.start()
    try:
        yield server
    finally:
        server.server.shutdown()
        server.server.server_close()
        server.thread.join()


def write_tarball(path, members):
    """A real .tgz laid out the way GitHub wraps one: <owner>-<repo>-<sha>/..."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in members.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path.write_bytes(buf.getvalue())


# The plugin every agent gets. Two files, and the manifest's `name:` line is what
# fetch-tree checks -- so this is a real fixture of the real contract, not a stub.
PLUGIN_TARBALL = {
    "plow-pbc-repo-abc1234/plow-chat-platform/plugin.yaml": "name: plow-chat-platform\nkind: platform\n",
    "plow-pbc-repo-abc1234/plow-chat-platform/__init__.py": "def register(ctx):\n    pass\n",
}

# The fleet skills every agent gets, at the paths the canonical copies keep in
# plow-pbc/plow's hosted-agent seed. Restore fetches them unconditionally, so
# the default `gh` serves them the way it serves the plugin -- otherwise every
# plain `run("restore", ...)` in the suite would fail on a fetch it never asked
# about. One tarball carries both trees: the real fetch is a whole-repo
# snapshot fetch-tree extracts a src subtree from.
FLEET_SEED = "cloud-agents/hermes/image/seed/skills"
FLEET_SKILL_SRC = f"{FLEET_SEED}/productivity/google-workspace"
FLEET_SKILL_TARBALL = {
    f"plow-pbc-plow-abc1234/{FLEET_SKILL_SRC}/SKILL.md": "---\nname: google-workspace\n---\n# google-workspace\n",
    f"plow-pbc-plow-abc1234/{FLEET_SEED}/growth/plow-invite/SKILL.md": "---\nname: plow-invite\n---\n# plow-invite\n",
    f"plow-pbc-plow-abc1234/{FLEET_SEED}/growth/plow-invite/scripts/mint_invite.py": "#!/usr/bin/env python3\n",
}


def install_gh_dispatching(b, *, plugin_tgz, fleet_tgz, skill_tgz=None):
    """A `gh` that answers by repo, because one invocation can need either.

    A skill test restores first -- which installs the plugin and the fleet
    skill -- and then adds a skill, so a `gh` that served one tarball to all
    would fail whichever came second on fetch-tree's manifest name check.
    Dispatching on the argv is what the real `gh api repos/<repo>/tarball/<ref>`
    does anyway.
    """
    other = f"cat {skill_tgz}" if skill_tgz else 'echo "no fake for: $*" >&2; exit 1'
    (b / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f"  *hermes-plow-chat*) cat {plugin_tgz} ;;\n"
        # Matches the repo alone, so a test adding a DIFFERENT skill sourced
        # from plow-pbc/plow would be served this tarball and fail fetch-tree's
        # name check confusingly -- give such a test its own dispatching gh.
        f"  *repos/plow-pbc/plow/tarball*) cat {fleet_tgz} ;;\n"
        f"  *) {other} ;;\n"
        "esac\n"
    )
    (b / "gh").chmod(0o755)


def _write_fleet_tgz(tmp_path):
    tgz = tmp_path / "fleet-skill.tgz"
    write_tarball(tgz, FLEET_SKILL_TARBALL)
    return tgz


def install_fake_gh(tmp_path, b):
    """The default: a `gh` that serves what every restore fetches and nothing else.

    Never overwrites one already there. This runs inside `run()`, so it fires on
    every invocation -- including the ones a skill test set up with a richer,
    skill-serving `gh` in this same bin directory. Clobbering that one made the
    skill install silently fetch the plugin tarball instead.
    """
    if (b / "gh").exists():
        return b
    tgz = tmp_path / "plugin.tgz"
    write_tarball(tgz, PLUGIN_TARBALL)
    install_gh_dispatching(b, plugin_tgz=tgz, fleet_tgz=_write_fleet_tgz(tmp_path))
    return b


def fake_skill_gh(tmp_path, *, skill_name="property-hunt", files=(), src=None):
    """A `gh` that serves a real tarball, so the REAL fetch-tree runs end to end.

    Only the gh half: pairing it with a RUNNING fake_docker is what lets a test
    reach add-skill's reload, which a non-running one exits before.
    """
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    root = "plow-pbc-repo-abc1234"
    prefix = f"{root}/{src}/" if src else f"{root}/"
    members = {f"{prefix}SKILL.md": f"---\nname: {skill_name}\n---\n# {skill_name}\n"}
    for name, body in files:
        members[f"{prefix}{name}"] = body

    skill_tgz = tmp_path / "skill.tgz"
    write_tarball(skill_tgz, members)
    plugin_tgz = tmp_path / "plugin.tgz"
    write_tarball(plugin_tgz, PLUGIN_TARBALL)
    # All three, because a skill test restores before it adds, and restore
    # installs the plugin and the fleet skill through this same installer.
    install_gh_dispatching(
        b, plugin_tgz=plugin_tgz, fleet_tgz=_write_fleet_tgz(tmp_path), skill_tgz=skill_tgz
    )
    return b
