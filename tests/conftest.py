import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def registry(tmp_path):
    """An isolated registry file; never the operator's real one."""
    return tmp_path / "config" / "agent-mgr" / "agents"


@pytest.fixture
def run(registry, tmp_path):
    """Invoke the real agent-mgr CLI with an isolated registry and HOME."""

    def _run(*args, env=None, check=False):
        e = dict(os.environ)
        e["AGENT_MGR_REGISTRY"] = str(registry)
        e["HOME"] = str(tmp_path / "home")
        (tmp_path / "home").mkdir(exist_ok=True)
        # restore installs the plugin through the same fetch-tree the skills
        # use, and activate still curls the activation script out of the
        # archived seed -- so both a hermetic `gh` and a hermetic `curl` are on
        # PATH for every invocation unless a test overrides PATH deliberately.
        b = fake_curl(tmp_path)
        install_fake_gh(tmp_path, b)
        e["PATH"] = f"{b}:{e['PATH']}"
        if env:
            e.update(env)
        return subprocess.run(
            [str(ROOT / "agent-mgr"), *args],
            capture_output=True, text=True, env=e, check=check,
        )

    return _run


@pytest.fixture
def instance(tmp_path):
    """Create an instance repo on disk and return its path."""

    def _instance(name, descriptor="", config="model:\n  provider: openai-codex\n"):
        repo = tmp_path / f"{name}-repo"
        repo.mkdir(exist_ok=True)
        (repo / "agent.env").write_text(descriptor)
        if config is not None:
            (repo / "config.yaml").write_text(config)
        return repo

    return _instance


def fake_docker(tmp_path, *, home, container="hermes-<name>", project="hermes-<name>",
                name="rowan", running=True, exec_output=None, log=None):
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
        f'printf "%s\\n" "$*" >> {log}' if log else "",
        'case "$*" in',
        f"  *\"config --format json\"*) cat <<'JSON'\n{cfg}\nJSON\n    ;;",
        f'  *"ps --status running --quiet"*) {"echo deadbeef" if running else ":"} ;;',
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
    "plow-pbc-repo-abc1234/plow-chat-platform/plugin.yaml":
        "name: plow-chat-platform\nkind: platform\n",
    "plow-pbc-repo-abc1234/plow-chat-platform/__init__.py":
        "def register(ctx):\n    pass\n",
}


def install_gh_dispatching(b, *, plugin_tgz, skill_tgz=None):
    """A `gh` that answers by repo, because one invocation can need either.

    A skill test restores first -- which installs the plugin -- and then adds a
    skill, so a `gh` that served one tarball to both would fail whichever came
    second on fetch-tree's manifest name check. Dispatching on the argv is what
    the real `gh api repos/<repo>/tarball/<ref>` does anyway.
    """
    other = f'cat {skill_tgz}' if skill_tgz else 'echo "no fake for: $*" >&2; exit 1'
    (b / "gh").write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        f"  *hermes-plow-chat*) cat {plugin_tgz} ;;\n"
        f"  *) {other} ;;\n"
        "esac\n"
    )
    (b / "gh").chmod(0o755)


def install_fake_gh(tmp_path, b):
    """The default: a `gh` that can serve the plugin and nothing else.

    Never overwrites one already there. This runs inside `run()`, so it fires on
    every invocation -- including the ones a skill test set up with a richer,
    skill-serving `gh` in this same bin directory. Clobbering that one made the
    skill install silently fetch the plugin tarball instead.
    """
    if (b / "gh").exists():
        return b
    tgz = tmp_path / "plugin.tgz"
    write_tarball(tgz, PLUGIN_TARBALL)
    install_gh_dispatching(b, plugin_tgz=tgz)
    return b


def fake_skill_bin(tmp_path, skill_name="property-hunt", extra_files=(), agent="property",
              subdirs=(), src=None):
    """A `gh` that serves a real tarball, so the REAL fetch-tree runs end to end.

    A tarball rather than a contents listing because that is what the installer
    now asks for -- and it is the only shape that can carry the nested
    directories the per-file version silently dropped.
    """
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    root = "plow-pbc-repo-abc1234"
    prefix = f"{root}/{src}/" if src else f"{root}/"
    members = {f"{prefix}SKILL.md": f"---\nname: {skill_name}\n---\n# {skill_name}\n"}
    for name, body in extra_files:
        members[f"{prefix}{name}"] = body
    for name, body in subdirs:
        members[f"{prefix}{name}"] = body

    skill_tgz = tmp_path / "skill.tgz"
    write_tarball(skill_tgz, members)
    plugin_tgz = tmp_path / "plugin.tgz"
    write_tarball(plugin_tgz, PLUGIN_TARBALL)
    # Both, because a skill test restores before it adds: restore installs the
    # plugin through the same installer.
    install_gh_dispatching(b, plugin_tgz=plugin_tgz, skill_tgz=skill_tgz)
    fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{agent}", name=agent,
                running=False)
    return {"PATH": f"{b}:{os.environ['PATH']}"}
