"""Break-glass on ardi: undo a settings change Jarvis (or anyone) made, with the core down.

    python3 scripts/admin_revert.py --list
    python3 scripts/admin_revert.py --last
    python3 scripts/admin_revert.py chg_...

It runs ``jarvis_core.admin_revert`` inside the production container's image against its data
volume, so it works even when the core will not start (docs/stories/14_agent_admin.md). Then:
``docker restart jarvis2-core``. Normally the change card's Revert or Settings -> Changes -> Undo
is enough - neither needs a model.
"""

from __future__ import annotations

import os
import subprocess
import sys

CONTAINER = os.environ.get("JARVIS_CONTAINER", "jarvis2-core")
IMAGE = os.environ.get("JARVIS_IMAGE", "jarvis2-core:latest")
VOLUME = os.environ.get("JARVIS_VOLUME", "jarvis2-data")


def main() -> int:
    args = sys.argv[1:]
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True, text=True, check=False
    ).stdout.strip()
    if running == "true":
        cmd = ["docker", "exec", CONTAINER, "/app/.venv/bin/python", "-m", "jarvis_core.admin_revert", *args]
    else:
        cmd = ["docker", "run", "--rm", "-v", f"{VOLUME}:/data", "-e", "JARVIS_HOME=/data", "--entrypoint",
               "/app/.venv/bin/python", IMAGE, "-m", "jarvis_core.admin_revert", *args]  # fmt: skip
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
