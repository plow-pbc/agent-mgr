import base64
import os

from conftest import fake_docker


def _fake_bin(tmp_path, skill_name="property-hunt", extra_files=(), agent="property"):
    """A gh that serves a real directory listing and per-file contents, so the
    REAL fetch-skill runs end to end without a network call."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    contents = {"SKILL.md": f"---\nname: {skill_name}\n---\n# {skill_name}\n"}
    for name, body in extra_files:
        contents[name] = body
    listing = "\n".join(contents)
    cases = "\n".join(
        '  *"contents/%s?ref"*|*"contents/"*"/%s?ref"*) echo %s ;;'
        % (n, n, base64.b64encode(v.encode()).decode())
        for n, v in contents.items()
    )
    (b / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        f'{cases}\n'
        f'  *contents*) printf "%s\\n" "{listing}" ;;\n'
        'esac\n'
    )
    (b / "gh").chmod(0o755)
    # No gateway running: add-skill installs, then reload-if-running exits 0
    # with nothing to reload. The config still has to parse -- every path goes
    # through resolve-guard now.
    fake_docker(tmp_path, home=tmp_path / "home" / f".hermes-{agent}", name=agent,
                running=False)
    return {"PATH": f"{b}:{os.environ['PATH']}"}


def test_add_skill_installs_into_the_agents_skills_directory(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    r = run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "a" * 40,
            "--dest", "productivity/property-hunt", env=_fake_bin(tmp_path))
    assert r.returncode == 0, r.stderr
    installed = (tmp_path / "home" / ".hermes-property" / "skills"
                 / "productivity" / "property-hunt" / "SKILL.md")
    assert installed.exists()
    assert "name: property-hunt" in installed.read_text()


def test_the_pin_is_recorded_in_the_instance_repo(run, instance, tmp_path):
    repo = instance("property")
    run("register", "property", str(repo))
    run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "a" * 40,
        "--dest", "productivity/property-hunt", env=_fake_bin(tmp_path))
    tsv = (repo / "skills.tsv").read_text()
    assert "plow-pbc/property-hunt" in tsv and "a" * 40 in tsv


def test_adding_the_same_skill_again_updates_the_pin_rather_than_duplicating(run, instance, tmp_path):
    repo = instance("property")
    run("register", "property", str(repo))
    for ref in ("a" * 40, "b" * 40):
        run("add-skill", "property", "plow-pbc/property-hunt", "--ref", ref,
            "--dest", "productivity/property-hunt", env=_fake_bin(tmp_path))
    tsv = (repo / "skills.tsv").read_text()
    assert tsv.count("plow-pbc/property-hunt") == 1
    assert "b" * 40 in tsv and "a" * 40 not in tsv


def test_a_branch_ref_is_refused(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    r = run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "main",
            env=_fake_bin(tmp_path))
    assert r.returncode != 0
    assert "40-char SHA" in r.stderr


def test_a_fetched_file_that_is_not_the_named_skill_is_refused(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    r = run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "a" * 40,
            "--dest", "productivity/property-hunt",
            env=_fake_bin(tmp_path, skill_name="something-else"))
    assert r.returncode != 0
    assert "not the property-hunt skill" in r.stderr


def test_a_refused_fetch_leaves_no_partial_skill_behind(run, instance, tmp_path):
    """Fetched to a temp dir and moved, so a bad fetch never truncates a running
    agent's skill."""
    run("register", "property", str(instance("property")))
    run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "a" * 40,
        "--dest", "productivity/property-hunt",
        env=_fake_bin(tmp_path, skill_name="something-else"))
    assert not (tmp_path / "home" / ".hermes-property" / "skills"
                / "productivity" / "property-hunt" / "SKILL.md").exists()


def test_an_unknown_option_is_refused_rather_than_ignored(run, instance, tmp_path):
    run("register", "property", str(instance("property")))
    r = run("add-skill", "property", "plow-pbc/property-hunt", "--branch", "main",
            env=_fake_bin(tmp_path))
    assert r.returncode != 0
    assert "unknown option" in r.stderr


def test_a_skill_whose_code_runs_in_the_container_gets_its_script_too(run, instance, tmp_path):
    """check-connectors execs plow_connector.py. A SKILL.md-only install would
    leave that command permanently broken with a generic "no such file"."""
    run("register", "rowan", str(instance("rowan")))
    r = run("add-skill", "rowan", "plow-pbc/seed-hermes-plow", "--ref", "a" * 40,
            "--dest", "plow-connectors",
            env=_fake_bin(tmp_path, skill_name="plow-connectors", agent="rowan",
                          extra_files=[("plow_connector.py", "#!/usr/bin/env python3\nprint('ok')\n")]))
    assert r.returncode == 0, r.stderr
    d = tmp_path / "home" / ".hermes-rowan" / "skills" / "plow-connectors"
    assert (d / "SKILL.md").exists()
    assert (d / "plow_connector.py").exists(), "the executable half was dropped"


def test_a_fetched_script_is_executable(run, instance, tmp_path):
    """An unreadable 644 script fails as 'no such file' from inside the
    container, which is the least informative failure available."""
    run("register", "rowan", str(instance("rowan")))
    run("add-skill", "rowan", "plow-pbc/seed-hermes-plow", "--ref", "a" * 40,
        "--dest", "plow-connectors",
        env=_fake_bin(tmp_path, skill_name="plow-connectors", agent="rowan",
                      extra_files=[("plow_connector.py", "print('ok')\n")]))
    script = tmp_path / "home" / ".hermes-rowan" / "skills" / "plow-connectors" / "plow_connector.py"
    assert script.stat().st_mode & 0o111, "the script is not executable"


def test_a_directory_with_no_skill_md_is_refused_by_name(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "gh").write_text('#!/usr/bin/env bash\nprintf "README.md\\n"\n')
    (b / "gh").chmod(0o755)
    (b / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
    (b / "docker").chmod(0o755)
    r = run("add-skill", "rowan", "plow-pbc/seed-hermes-plow", "--ref", "a" * 40,
            "--dest", "plow-connectors", env={"PATH": f"{b}:{os.environ['PATH']}"})
    assert r.returncode != 0
    assert "no SKILL.md" in r.stderr
