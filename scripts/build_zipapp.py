#!/usr/bin/env python3
"""Build the deliberately small, self-extracting agent-mgr release artifact."""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(arguments: list[str]) -> int:
    output = Path(arguments[0] if arguments else ROOT / "dist" / "agent-mgr.pyz").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-mgr-build-") as temporary:
        stage = Path(temporary)
        for file in ("__main__.py", "agent-mgr", "LICENSE", "NOTICE"):
            shutil.copy2(ROOT / file, stage / file)
        for directory in ("agent_mgr", "lib", "runtime", "templates"):
            shutil.copytree(ROOT / directory, stage / directory)
        zipapp.create_archive(stage, output, interpreter="/usr/bin/env python3")
    output.chmod(0o755)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
