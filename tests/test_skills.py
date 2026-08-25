import base64
import os


def _fake_bin(tmp_path, skill_name="property-hunt"):
    """A gh that returns a base64 SKILL.md and a docker that reports nothing
    running, so the REAL fetch-skill runs end to end without a network call."""
    body = base64.b64encode(f"---\nname: {skill_name}\n---\n# {skill_name}\n".encode()).decode()
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "gh").write_text(f"#!/usr/bin/env bash\necho '{body}'\n")
    (b / "gh").chmod(0o755)
    (b / "docker").write_text("#!/usr/bin/env bash\nexit 0\n")
    (b / "docker").chmod(0o755)
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
