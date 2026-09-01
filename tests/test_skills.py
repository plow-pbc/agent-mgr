import base64
import os

import pytest
from conftest import ROOT, fake_docker, fake_skill_gh


def _fake_bin(tmp_path, skill_name="property-hunt", files=(), agent="property", src=None):
    """The gh tarball plus a NOT-running docker: these tests assert what the
    installer wrote, not what the reload did."""
    b = fake_skill_gh(tmp_path, skill_name=skill_name, files=files, src=src)
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


def test_a_failed_manifest_publish_preserves_existing_pins(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT))
    from agent_mgr import commands

    manifest = tmp_path / "skills.tsv"
    manifest.write_text("existing\n")
    monkeypatch.setattr(commands, "atomic_write",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(commands.AgentMgrError, match="full"):
        commands._write_manifest(manifest, "replacement\n")
    assert manifest.read_text() == "existing\n"


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
    assert "does not name property-hunt" in r.stderr


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
                          files=[("plow_connector.py", "#!/usr/bin/env python3\nprint('ok')\n")]))
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
                      files=[("plow_connector.py", "print('ok')\n")]))
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
            env=_fake_bin(tmp_path, files=[
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
        env=_fake_bin(tmp_path, files=[("old.py", "print('stale')\n")]))
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
                          files=[("plow_connector.py", "print('ok')\n")]))
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


def test_two_skills_from_one_monorepo_keep_both_pins(run, instance, tmp_path):
    """The manifest was keyed on the REPO, so adding the second skill from one
    monorepo deleted the first's pin and a clean deploy silently omitted it."""
    repo = instance("property")
    run("register", "property", str(repo))
    for dest, src in (("first", "ref/a"), ("second", "ref/b")):
        run("add-skill", "property", "plow-pbc/mono", "--ref", "a" * 40,
            "--dest", dest, "--src", src,
            env=_fake_bin(tmp_path, skill_name=dest, src=src))
    rows = (repo / "skills.tsv").read_text().splitlines()
    assert len(rows) == 2, f"a pin was dropped: {rows}"
    assert any("\tfirst\t" in r for r in rows) and any("\tsecond\t" in r for r in rows)


@pytest.mark.parametrize(
    ("dest", "name"),
    [
        ("productivity/google-workspace", "google-workspace"),
        ("growth/plow-invite", "plow-invite"),
    ],
)
def test_deploy_installs_every_fleet_skill(run, instance, tmp_path, dest, name):
    """Every agent gets every pinned fleet skill: google-workspace replaces the
    image-bundled copy whose local-OAuth path no instance has; plow-invite is
    the delight-triggered referral capability (plow-pbc/agent-mgr#72)."""
    run("register", "rowan", str(instance("rowan")))
    r = run("deploy", "rowan")
    assert r.returncode == 0, r.stderr
    installed = tmp_path / "home" / ".hermes-rowan" / "skills" / dest / "SKILL.md"
    assert installed.exists()
    assert f"name: {name}" in installed.read_text()


def test_deploy_skips_the_fleet_skill_when_the_instance_pins_it(run, instance, tmp_path):
    """Installing the fleet copy first would leave it deployed over a working
    instance copy whenever the later replay's fetch fails mid-deploy, so
    deploy does not install the fleet copy at all for an instance-owned dest."""
    repo = instance("property")
    (repo / "skills.tsv").write_text(
        f"plow-pbc/property-hunt\t{'a' * 40}\tproductivity/google-workspace\t\n"
    )
    run("register", "property", str(repo))
    r = run("deploy", "property",
            env=_fake_bin(tmp_path, skill_name="google-workspace",
                          files=(("INSTANCE.md", "instance copy"),)))
    assert r.returncode == 0, r.stderr
    assert "skipped" in r.stdout
    installed = (tmp_path / "home" / ".hermes-property" / "skills"
                 / "productivity" / "google-workspace")
    assert (installed / "INSTANCE.md").exists()


def test_install_skill_skips_an_instance_pinned_dest_and_installs_the_rest(run, instance, tmp_path):
    """deploy deliberately installs an instance's own skills.tsv copy last; a
    standalone fleet install over it would contradict the reviewed pin until
    the next deploy silently flipped it back. With more than one fleet skill,
    the owned dest is skipped per skill while the others still install."""
    repo = instance("rowan")
    (repo / "skills.tsv").write_text(
        f"plow-pbc/x\t{'a' * 40}\tproductivity/google-workspace\t\n"
    )
    run("register", "rowan", str(repo))
    run("deploy", "rowan", env=_fake_bin(tmp_path, skill_name="google-workspace", agent="rowan"))
    r = run("install-skill", "rowan")
    assert r.returncode == 0, r.stderr
    assert "skipped" in r.stdout and "google-workspace" in r.stdout
    installed = tmp_path / "home" / ".hermes-rowan" / "skills" / "growth" / "plow-invite" / "SKILL.md"
    assert installed.exists()


def test_a_dotted_destination_does_not_accept_a_different_skill(run, instance, tmp_path):
    """`foo.bar` reached an ERE, so `name: fooXbar` satisfied the check that
    exists to prove the fetched tree is the skill this agent pinned."""
    run("register", "property", str(instance("property")))
    r = run("add-skill", "property", "plow-pbc/x", "--ref", "b" * 40, "--dest", "foo.bar",
            env=_fake_bin(tmp_path, skill_name="fooXbar"))
    assert r.returncode != 0, "installed a skill whose name only matched as a regex"
    # fetch-tree serves both roots, so its refusal names the manifest rather
    # than the word "skill" -- it still names the destination, which is what
    # tells this refusal apart from a fetch that simply failed.
    assert "does not name foo.bar" in r.stderr
