import contextlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The assistant wire contract, in the one place the suite reads it: every route
# is exercised against these exact resources, so a shape that drifts reddens
# every cloud module at once rather than whichever one loaded it.
ASSISTANT_CONTRACT = json.loads(
    (ROOT / "tests/fixtures/assistant-contract.json").read_text(encoding="utf-8")
)

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
   "environment": {"AGENT_ID": "${AGENT_NAME:-unset}"},
   "volumes": [{"target": "${AGENT_HOME_TARGET:-/opt/data}", "source": "${AGENT_HOME:-unset}"}]}}}
JSON
    ;;
  # The image's baked HERMES_HOME -- the one fact the whole boot-contract
  # derivation reads. Every fixture agent is legacy by default; a test that
  # needs the current contract builds its own docker with fake_docker().
  *"Config.Env"*) echo '["HERMES_HOME=/opt/data"]' ;;
  *"image inspect"*) ;;
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
                    "Pass os.environ['PATH'] to inherit the suite's stub.")
            assert _docker_the_suite_owns(path), (
                f"this env resolves docker to {shutil.which('docker', path=path)}, "
                "which the suite did not create; build PATH as "
                "f\"{mybin}:{os.environ['PATH']}\" so the stub still wins, or "
                "use conftest.allow_real_docker()")
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
        # deploy fetches an agent's skills.tsv pins through fetch-tree, and
        # activate curls the activation script -- so both a hermetic `gh` and a
        # hermetic `curl` are on PATH for every invocation unless a test
        # overrides PATH deliberately.
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


def fake_docker(tmp_path, *, home, container="hermes-<name>", project="hermes-<name>",
                name="rowan", running=True, exec_output=None, log=None, mount=None,
                exists=None, all_cids=(), mounts=None, image=None, build=False,
                pull_policy=None, home_env="/opt/data", container_home_env=None,
                relay_env=None):
    """A `docker` that answers the four things agent-mgr asks of it.

    One builder rather than one per test file: every command now passes through
    resolve-guard, so every fake needs a parseable `config --format json` -- and
    three near-copies of that JSON drift the moment the guard reads a new field.

    `home_env` is the image's baked HERMES_HOME -- legacy (/opt/data) by
    default, matching the fleet's real fixtures. It drives BOTH the mount
    target compose would resolve and the boot-contract derivation's own
    `docker inspect`, so the two stay consistent the way the real image is.

    `container_home_env` is the EXISTING container's own baked HERMES_HOME,
    for the mid-migration case where it differs from the replacement image's:
    it also moves the mount the container genuinely carries, so a check that
    searched at the image's target instead finds nothing.

    `log` records argv when given, so a test can assert on what actually ran
    rather than on what the source says.

    `relay_env` is the CONTAINER's own environment -- what an override's
    env_file or `environment:` block put there. `check-latch` resolves its
    credential from the home dotenv first and this second, the way hermes does,
    so a fixture that could only express the dotenv could not tell the two
    apart.
    """
    import json

    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    container = container.replace("<name>", name)
    project = project.replace("<name>", name)
    svc = {
        "container_name": container,
        # resolve_guard checks this against the registry name: the override
        # merges after the template and can replace it, and a forged one
        # attributes usage to a sibling. A fake that omits it would leave that
        # guard asserting nothing.
        "environment": {"AGENT_ID": name},
        "volumes": [{"target": home_env, "source": str(home)}],
    }
    if home_env == "/var/lib/hermes":
        # What compose.current.yml renders from AGENT_CREDENTIALS_HOST, which
        # resolves against the subprocess HOME the `run` fixture sets -- the
        # parent of every fixture agent's home. resolve_guard now checks it.
        svc["volumes"].append({"target": "/var/lib/plow/credentials.host",
                               "source": str(Path(home).parent / f".plow-credentials-{name}"),
                               "read_only": True})
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
        # A container that predates a contract change, answering for its own
        # baked home and mounting there -- before the image answers below,
        # which are broader and would otherwise swallow it.
        (f'case "$*" in *"Config.Env"*deadbeef*) echo \'["HERMES_HOME={container_home_env}"]\'; '
         'exit 0 ;; esac\n'
         f'case "$*" in *Mounts*) case "$*" in *\'"{container_home_env}"\'*) echo {home} ;; esac; '
         'exit 0 ;; esac') if container_home_env else "",
        # The image's own, unconditional: the boot-contract derivation's
        # own `docker inspect --format {{json .Config.Env}}` call, for an image
        # ref OR a running container id alike. Must win over the mount-echo
        # shortcuts below, which answer a DIFFERENT inspect format
        # (container Mounts) and would otherwise swallow this one too.
        f'case "$*" in *"Config.Env"*) echo \'["HERMES_HOME={home_env}"]\'; exit 0 ;; esac',
        f'case "$*" in *inspect*) echo "{mount}"; exit 0 ;; esac' if mount is not None else "",
        f'printf "%s\\n" "$*" >> {log}' if log else "",
        # And one word per line beside it: the joined form cannot tell an intact
        # argv word from one the caller re-split, which is what the sh -c escape
        # needs observed. Separate file so substring assertions on the joined
        # log keep working.
        f'printf "%s\\n" "$@" >> {log}.argv' if log else "",
        # stdin is captured ALWAYS, not only when `log` is set: for `sh -s` the
        # piped bytes are the script, and running it is what makes these tests
        # assert about the probe instead of about a canned reply. Gated on
        # `exec -T` because that is what the real command needs to forward a
        # pipe, and on fd 0 not being a terminal, because most execs here
        # inherit the parent's stdin -- which under `pytest -s` IS the
        # terminal, and an unconditional `cat` would hang the suite.
        f'stdin_capture="{b}/.stdin.$$"',
        'case "$*" in *"exec -T"*) [ -t 0 ] || cat > "$stdin_capture" ;; esac',
        # stdin beside argv, in its OWN file: a test asserting a secret is
        # absent from argv proves nothing about whether it still reaches the
        # command, and one file could not tell the two apart.
        (f'[ -s "$stdin_capture" ] && cat "$stdin_capture" >> {log}.stdin'
         if log else ""),
        'case "$*" in',
        f"  *\"config --format json\"*) cat <<'JSON'\n{cfg}\nJSON\n    ;;",
        # `ps -a` answers about EXISTENCE, `--status running` about running.
        # They differ for a stopped container, which is the case the identity
        # seam was blind to, so a test can now set them independently.
        f'  *"ps -a --quiet"*) {"printf '%s\\n' " + " ".join(all_cids) if all_cids else ("echo deadbeef" if (running if exists is None else exists) else ":")} ;;',
        f'  *"ps --status running --quiet"*) {"echo deadbeef" if running else ":"} ;;',
        (f'  *inspect*) case "$*" in ' + " ".join(
            f'*{c}*) echo {m} ;;' for c, m in (mounts or {}).items())
         + f' *) echo {home} ;; esac ;;') if mounts else f'  *inspect*) echo {home} ;;',
    ]
    # `check-latch` runs `exec -T hermes sh -s`, whose script is the piped
    # stdin. Run it for real, with the container's own environment and a curl
    # that answers `exec_output`, so the probe's own logic is under test.
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    curl_log = f'printf "%s\\n" "$@" >> {log}.curlargv' if log else ":"
    # The probe pipes its curl config on stdin (`--config -`) rather than
    # writing it to disk. A test asserting the bearer is off argv proves
    # nothing about whether it still reached curl at all -- this is the
    # positive half, read off the same pipe curl itself would consume.
    config_log = f'case "$*" in *--config*) cat >> {log}.curlconfig ;; esac' if log else ":"
    (stub / "curl").write_text(
        f'#!/usr/bin/env bash\n{curl_log}\n{config_log}\nprintf "%s" {exec_output or ""}\n')
    (stub / "curl").chmod(0o755)
    env_prefix = " ".join(
        f"{key}={shlex.quote(value)}" for key, value in sorted((relay_env or {}).items()))
    # `-i`: a developer with DOMO_DEVICE_UID set in their own shell must not
    # silently satisfy the container-env fallback this fixture exists to
    # exercise -- relay_env is the container's WHOLE environment, not an
    # addition to whatever the test happens to be running under.
    # Piped in, not passed as a file argument: production's `sh -s` reads the
    # script off the same fd `curl --config -` later reads its config from,
    # and only piping here exercises that curl does not eat the rest of the
    # script when it shares that fd.
    parts.append(
        f'  *"sh -s"*) cat "$stdin_capture" | env -i {env_prefix} PATH="{stub}:$PATH" sh -s ;;')
    if exec_output is not None:
        parts.append(f'  *exec*) echo {exec_output} ;;')
    parts += ["esac", "exit 0", ""]
    (b / "docker").write_text("\n".join(x for x in parts if x))
    (b / "docker").chmod(0o755)
    return b


def shlex_quote(s):
    import shlex
    return shlex.quote(s)


def fake_curl(tmp_path, *, body="#!/usr/bin/env bash\nexit 0\n", fail=False):
    """A `curl -o <path>` that writes a no-op plugin installer.

    deploy installs the plugin, so the real path curls upstream. Stubbing curl
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


def install_fake_gh(tmp_path, b):
    """The default: a `gh` that serves nothing. Deploy fetches only what an
    agent's own skills.tsv pins, so a test that needs a tarball installs a
    serving `gh` (fake_skill_gh) first -- and this never overwrites one
    already there, since it runs inside `run()` on every invocation.
    """
    if (b / "gh").exists():
        return b
    (b / "gh").write_text('#!/usr/bin/env bash\necho "no fake for: $*" >&2; exit 1\n')
    (b / "gh").chmod(0o755)
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
    (b / "gh").write_text(f"#!/usr/bin/env bash\ncat {skill_tgz}\n")
    (b / "gh").chmod(0o755)
    return b
