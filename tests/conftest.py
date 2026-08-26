import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The PATH the suite inherited, before it was made docker-free. Only
# tests/test_compose.py uses it -- see the fixture below.
REAL_PATH = os.environ.get("PATH", "")

# The real docker, resolved once against the PATH above -- before the session
# fixture shadows it. What the `run` fixture below asserts no test can reach.
REAL_DOCKER = shutil.which("docker", path=REAL_PATH)

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
   "volumes": [{"target": "/opt/data", "source": "${AGENT_HOME:-unset}"}]}}}
JSON
    ;;
  *"ps --status running --quiet"*) ;;
  *inspect*)
    # Self-consistent by default, like the config above: the container this
    # agent would find is the one mounting this agent's home.
    echo "${FAKE_MOUNT:-${AGENT_HOME:-unset}}" ;;
  *)
    echo "refusing a docker call a test did not stub: docker $*" >&2
    exit 97 ;;
esac
exit 0
"""


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
    b = tmp_path_factory.mktemp("poison-bin")
    (b / "docker").write_text(SAFE_DOCKER)
    (b / "docker").chmod(0o755)
    # Prepended, not filtered: `docker` lives in /usr/bin beside python3, bash
    # and every other tool the suite shells out to, so removing the directory
    # removes the suite. Shadowing is enough -- a test building
    # f"{mybin}:{os.environ['PATH']}" still puts this ahead of /usr/bin.
    os.environ["PATH"] = os.pathsep.join([str(b), REAL_PATH])
    yield


@pytest.fixture
def registry(tmp_path):
    """An isolated registry file; never the operator's real one."""
    return tmp_path / "config" / "agent-mgr" / "agents"


@pytest.fixture
def run(registry, tmp_path):
    """Invoke the real agent-mgr CLI with an isolated registry and HOME."""
    # A docker that REFUSES, installed once before any test body runs.
    #
    # This suite was hermetic in every dimension it thought to isolate and not
    # in the one that mattered: AGENT_PROJECT defaults to `hermes-<name>`, so a
    # fixture agent called `rowan` or `str` resolves to the LIVE compose
    # project. A test reaching the real daemon therefore did not fail -- it
    # restarted production. One run issued 20 `compose restart hermes` calls
    # against live projects; over a day of PR iteration that was 917 boots of
    # the rentals gateway, 1,378 of rowan's, and 207 shutdown notices into an
    # owners' channel (plow-pbc/agent-mgr#13).
    #
    # Poisoning PATH kills the class rather than the test that surfaced it:
    # pinning the project name would fix today's collision and leave the suite
    # one rename away from the next. Tests that legitimately exercise compose
    # install fake_docker into this same directory, which replaces this file.
    poison = tmp_path / "bin"
    poison.mkdir(exist_ok=True)
    (poison / "docker").write_text(SAFE_DOCKER)
    (poison / "docker").chmod(0o755)

    def _run(*args, env=None, check=False):
        e = dict(os.environ)
        e["AGENT_MGR_REGISTRY"] = str(registry)
        e["HOME"] = str(tmp_path / "home")
        (tmp_path / "home").mkdir(exist_ok=True)
        # restore installs the plugin, so a hermetic curl is on PATH for every
        # invocation unless a test overrides PATH deliberately.
        b = fake_curl(tmp_path)
        e["PATH"] = f"{b}:{e['PATH']}"
        if env:
            e.update(env)
        # Asserted on RESOLUTION, not membership: a PATH built from scratch
        # drops the shadow, and one that merely puts /usr/bin first keeps it in
        # the list while still resolving to the real binary. Both end the same
        # way -- a green suite restarting a live gateway -- so the check is
        # simply "which docker would this env find?". Here, where every `run`
        # override lands, so the next one is red when it is written.
        found = shutil.which("docker", path=e["PATH"])
        assert found != REAL_DOCKER, (
            f"this env= override resolves docker to {found}, the operator's "
            "real one; build PATH as f\"{mybin}:{os.environ['PATH']}\"")
        return subprocess.run(
            [str(ROOT / "agent-mgr"), *args],
            capture_output=True, text=True, env=e, check=check,
        )

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
                name="rowan", running=True, exec_output=None, log=None, mount=None):
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
    cfg = json.dumps({
        "name": project,
        "services": {"hermes": {
            "container_name": container,
            "volumes": [{"target": "/opt/data", "source": str(home)}],
        }},
    })
    parts = [
        "#!/usr/bin/env bash",
        f'case "$*" in *inspect*) echo "{mount or ""}"; exit 0 ;; esac' if mount else "",
        f'printf "%s\\n" "$*" >> {log}' if log else "",
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
        f'  *"ps --status running --quiet"*) {"echo deadbeef" if running else ":"} ;;',
        f'  *inspect*) echo {home} ;;',
    ]
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




def fake_skill_gh(tmp_path, *, skill_name="property-hunt", files=(), src=None):
    """A `gh` that serves a real tarball, so the REAL fetch-skill runs end to end.

    Only the gh half: pairing it with a RUNNING fake_docker is what lets a test
    reach add-skill's reload, which a non-running one exits before.
    """
    import io
    import tarfile

    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    root = "plow-pbc-repo-abc1234"
    prefix = f"{root}/{src}/" if src else f"{root}/"
    members = {f"{prefix}SKILL.md": f"---\nname: {skill_name}\n---\n# {skill_name}\n"}
    for name, body in files:
        members[f"{prefix}{name}"] = body

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in members.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    (tmp_path / "skill.tgz").write_bytes(buf.getvalue())

    (b / "gh").write_text(f'#!/usr/bin/env bash\ncat {tmp_path / "skill.tgz"}\n')
    (b / "gh").chmod(0o755)
    return b
