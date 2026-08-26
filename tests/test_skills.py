import base64
import os

from conftest import fake_docker, fake_skill_gh


def _fake_bin(tmp_path, skill_name="property-hunt", extra_files=(), agent="property",
              subdirs=(), src=None):
    """The gh tarball plus a NOT-running docker: these tests assert what the
    installer wrote, not what the reload did."""
    b = fake_skill_gh(tmp_path, skill_name=skill_name, extra_files=extra_files,
                      subdirs=subdirs, src=src)
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


def test_a_tree_with_no_skill_md_is_refused_by_name(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    env = _fake_bin(tmp_path, skill_name="ignored", agent="rowan")
    # Rebuild the tarball without a SKILL.md.
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"# not a skill\n"
        info = tarfile.TarInfo("plow-pbc-repo-abc1234/README.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    (tmp_path / "skill.tgz").write_bytes(buf.getvalue())
    r = run("add-skill", "rowan", "plow-pbc/seed-hermes-plow", "--ref", "a" * 40,
            "--dest", "plow-connectors", env=env)
    assert r.returncode != 0
    assert "no SKILL.md" in r.stderr


def test_a_nested_directory_is_installed_not_silently_dropped(run, instance, tmp_path):
    """property-hunt keeps references/ and scripts/. A per-file listing recorded
    its pin while installing a tree that did not contain them."""
    run("register", "property", str(instance("property")))
    r = run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "a" * 40,
            "--dest", "productivity/property-hunt",
            env=_fake_bin(tmp_path, subdirs=[
                ("scripts/scrape.ts", "export const x = 1\n"),
                ("references/notes.md", "# notes\n"),
            ]))
    assert r.returncode == 0, r.stderr
    d = tmp_path / "home" / ".hermes-property" / "skills" / "productivity" / "property-hunt"
    assert (d / "scripts" / "scrape.ts").exists(), "a nested directory was dropped"
    assert (d / "references" / "notes.md").exists()


def test_a_file_removed_upstream_does_not_survive_the_next_install(run, instance, tmp_path):
    """An overlay leaves the old tree in place, so a skill keeps executing code
    the pinned ref deleted."""
    run("register", "property", str(instance("property")))
    run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "a" * 40,
        "--dest", "productivity/property-hunt",
        env=_fake_bin(tmp_path, extra_files=[("old.py", "print('stale')\n")]))
    d = tmp_path / "home" / ".hermes-property" / "skills" / "productivity" / "property-hunt"
    assert (d / "old.py").exists()
    run("add-skill", "property", "plow-pbc/property-hunt", "--ref", "b" * 40,
        "--dest", "productivity/property-hunt", env=_fake_bin(tmp_path))
    assert not (d / "old.py").exists(), "a file the new ref deleted survived"
    assert (d / "SKILL.md").exists()


def test_a_subpath_install_takes_only_that_subtree(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("add-skill", "rowan", "plow-pbc/seed-hermes-plow", "--ref", "a" * 40,
            "--dest", "plow-connectors", "--src", "ref/hermes-skill/plow-connectors",
            env=_fake_bin(tmp_path, skill_name="plow-connectors", agent="rowan",
                          src="ref/hermes-skill/plow-connectors",
                          extra_files=[("plow_connector.py", "print('ok')\n")]))
    assert r.returncode == 0, r.stderr
    d = tmp_path / "home" / ".hermes-rowan" / "skills" / "plow-connectors"
    assert (d / "SKILL.md").exists() and (d / "plow_connector.py").exists()


def test_a_traversing_destination_is_refused(run, instance, tmp_path):
    """The dest is joined onto the agent's home and the result is rm -rf'd during
    the swap, so `--dest ../../.ssh` is a delete primitive pointed at $HOME."""
    ssh = tmp_path / "home" / ".ssh"
    ssh.mkdir(parents=True)
    (ssh / "authorized_keys").write_text("ssh-ed25519 AAAA operator\n")
    run("register", "rowan", str(instance("rowan")))
    r = run("add-skill", "rowan", "plow-pbc/x", "--ref", "a" * 40,
            "--dest", "../../.ssh", env=_fake_bin(tmp_path, agent="rowan"))
    assert r.returncode != 0
    assert "may not traverse" in r.stderr
    assert (ssh / "authorized_keys").exists(), "the operator's keys were deleted"


def test_an_absolute_destination_is_refused(run, instance, tmp_path):
    run("register", "rowan", str(instance("rowan")))
    r = run("add-skill", "rowan", "plow-pbc/x", "--ref", "a" * 40,
            "--dest", "/etc/cron.d", env=_fake_bin(tmp_path, agent="rowan"))
    assert r.returncode != 0
    assert "must be relative" in r.stderr


def test_a_dotted_name_is_not_mistaken_for_traversal(run, instance, tmp_path):
    """Rejected by component, not by substring: `..foo` is a legitimate name."""
    run("register", "rowan", str(instance("rowan")))
    r = run("add-skill", "rowan", "plow-pbc/x", "--ref", "a" * 40, "--dest", "..foo",
            env=_fake_bin(tmp_path, skill_name="..foo", agent="rowan"))
    assert r.returncode == 0, r.stderr


def test_a_failed_publish_leaves_the_previous_skill_installed(run, instance, tmp_path):
    """Deleting the target before publishing meant a SIGTERM between the two left
    the agent with no skill at all -- worse than the stale one it replaced."""
    run("register", "rowan", str(instance("rowan")))
    env = _fake_bin(tmp_path, skill_name="s", agent="rowan")
    run("add-skill", "rowan", "plow-pbc/x", "--ref", "a" * 40, "--dest", "s", env=env)
    d = tmp_path / "home" / ".hermes-rowan" / "skills" / "s"
    assert (d / "SKILL.md").exists()
    # Make the publish fail: a directory in the way that mv cannot replace.
    (d / "SKILL.md").write_text("---\nname: s\n---\nthe good copy\n")
    import os
    os.chmod(d.parent, 0o555)
    try:
        r = run("add-skill", "rowan", "plow-pbc/x", "--ref", "b" * 40, "--dest", "s", env=env)
        assert r.returncode != 0
    finally:
        os.chmod(d.parent, 0o755)
    assert (d / "SKILL.md").read_text().endswith("the good copy\n"), "the previous copy was lost"
