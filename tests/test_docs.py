"""HOWTO delegates three contracts to the README; this checks the links land.

Deliberately literal. Two review rounds were spent on a regex that tried to
recognise any markdown link into the README, and each round found another
spelling it mis-parsed -- an underscored anchor dropped, then a path that
matched but never resolved. The contract here is not "validate arbitrary
markdown"; it is "these three specific delegations exist and point at these
three specific headings". Asserting the exact strings needs no parser, so there
is no alphabet to disagree on, no path to leave unresolved, and no spelling that
can fall outside a pattern and be counted as nothing to check.

The copies these back-references replaced were per-section, so this is too:
re-level or rename any one of the three README headings and its own delegation
fails by name, rather than the set merely shrinking.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# anchor -> the README heading it must resolve to. A fourth delegation is an
# explicit edit here, which is the point: what HOWTO stops restating is a
# decision, not something to be discovered by a scanner.
DELEGATIONS = {
    "three-layers-and-what-belongs-in-each": "## Three layers, and what belongs in each",
    "what-an-instance-repo-contains": "### What an instance repo contains",
    "why-agent-uses-exec": "## Why `agent` uses `exec`",
}


def github_slug(heading: str) -> str:
    """GitHub's anchor rule: lowercase, drop punctuation, spaces to hyphens."""
    text = heading.lstrip("#").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def test_every_contract_howto_delegates_still_resolves():
    readme = (ROOT / "README.md").read_text()
    howto = (ROOT / "docs" / "HOWTO.md").read_text()

    for anchor, heading in DELEGATIONS.items():
        assert heading in readme, (
            f"README.md no longer has the heading {heading!r}, which "
            f"docs/HOWTO.md delegates to as #{anchor}")
        # Pins the slug rule itself: renaming a heading and its link in step
        # still fails if the anchor is not what GitHub would generate.
        assert github_slug(heading) == anchor, (
            f"{heading!r} slugs to #{github_slug(heading)}, not #{anchor}")
        # The exact spelling, because it is the one docs/HOWTO.md resolves from.
        link = f"](../README.md#{anchor})"
        assert link in howto, (
            f"docs/HOWTO.md stopped delegating {heading!r} -- expected a link "
            f"ending {link}. Restore it, or drop {anchor!r} from DELEGATIONS.")
