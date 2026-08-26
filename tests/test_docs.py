"""The README is the canonical home for three contracts; HOWTO points at them.

Those pointers are load-bearing and silent when they break: re-level or rename a
README heading and the HOWTO section becomes a heading, a dead link and no
content, with nothing else in the suite noticing. Before the docs were
de-duplicated each fact had a local copy that could not rot out from under its
reader, so this test is what replaces that safety.

The copies it replaced were per-section, so this checks per-section too, and it
is deliberately permissive about what counts as a link into the README: a form
it cannot interpret has to FAIL, not fall outside the pattern and be counted as
nothing to check. A guard whose response to unparseable input is to skip it
reports green on exactly the rot it exists to catch.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Any relative spelling, anchor optional -- both so they can be rejected below.
LINK = re.compile(r"\]\((?:\.{1,2}/)*README\.md(#[^)\s]*)?\)")

# The three contracts HOWTO delegates rather than restates. Pinned as a set, not
# merely counted: dropping two of three would leave a non-empty set and a green
# suite, which is the same failure at 2/3 scale. Adding a fourth delegation is
# then an explicit edit here.
DELEGATED = {
    "three-layers-and-what-belongs-in-each",
    "what-an-instance-repo-contains",
    "why-agent-uses-exec",
}


def github_slug(heading: str) -> str:
    """GitHub's anchor rule: lowercase, drop punctuation, spaces to hyphens."""
    text = heading.lstrip("#").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def test_every_howto_link_into_the_readme_resolves():
    readme = (ROOT / "README.md").read_text()
    anchors = {github_slug(line) for line in readme.splitlines() if line.startswith("#")}
    howto = (ROOT / "docs" / "HOWTO.md").read_text()

    found = LINK.findall(howto)
    assert found, "docs/HOWTO.md no longer links into README.md at all"

    # A link with no anchor lands the reader at the top of a 150-line file and
    # cannot be checked against anything, so it is a failure rather than a skip.
    assert not [m for m in found if m in ("", "#")], (
        "docs/HOWTO.md links to README.md with no anchor -- name the section")

    referenced = {m.lstrip("#") for m in found}

    missing = sorted(referenced - anchors)
    assert not missing, (
        f"docs/HOWTO.md links to README anchors that do not exist: {missing}. "
        f"README currently offers: {sorted(anchors)}"
    )

    dropped = sorted(DELEGATED - referenced)
    assert not dropped, (
        f"docs/HOWTO.md stopped delegating to the README for: {dropped}. "
        "Either restore the back-reference or remove it from DELEGATED here."
    )
