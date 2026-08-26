"""The README is the canonical home for three contracts; HOWTO points at them.

Those pointers are load-bearing and silent when they break: re-level or rename a
README heading and the HOWTO section becomes a heading, a dead link and no
content, with nothing else in the suite noticing. Before the docs were
de-duplicated each fact had a local copy that could not rot out from under its
reader, so this test is what replaces that safety.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(\.\./README\.md#([a-z0-9-]+)\)")


def github_slug(heading: str) -> str:
    """GitHub's anchor rule: lowercase, drop punctuation, spaces to hyphens."""
    text = heading.lstrip("#").strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text)


def test_every_howto_link_into_the_readme_resolves():
    readme = (ROOT / "README.md").read_text()
    anchors = {github_slug(line) for line in readme.splitlines() if line.startswith("#")}

    howto = (ROOT / "docs" / "HOWTO.md").read_text()
    referenced = set(LINK.findall(howto))
    # The pointers are the point: a HOWTO that stopped linking to the README
    # would pass an emptily-quantified check while the sections it delegates
    # sat headed and bodyless.
    assert referenced, "docs/HOWTO.md no longer links into README.md"

    missing = sorted(referenced - anchors)
    assert not missing, (
        f"docs/HOWTO.md links to README anchors that no longer exist: {missing}. "
        f"README currently offers: {sorted(anchors)}"
    )
