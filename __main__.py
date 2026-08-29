"""Self-extracting entry point for the agent-mgr zipapp release."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> int:
    archive = Path(sys.argv[0]).resolve()
    if not zipfile.is_zipfile(archive):
        from agent_mgr.cli import main as cli_main

        return cli_main(sys.argv[1:])
    with tempfile.TemporaryDirectory(prefix="agent-mgr-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(root)
        for helper in (root / "lib").iterdir():
            if helper.is_file():
                helper.chmod(helper.stat().st_mode | 0o700)
        return subprocess.run(
            [sys.executable, str(root / "agent-mgr"), *sys.argv[1:]], check=False
        ).returncode


raise SystemExit(main())
