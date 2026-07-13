"""Render a redacted Obelisk operational alert as a local macOS notification."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any


def main() -> int:
    """Read an operation report from stdin and notify the logged-in user."""
    report: dict[str, Any] = json.load(sys.stdin)
    title = "Obelisk Memory: attention required"
    failed = [
        str(step.get("name", "step"))
        for step in report.get("steps", [])
        if not step.get("ok")
    ]
    message = ("Failed: " + ", ".join(failed[:3])) if failed else "Operation reported a failure."
    safe_message = message.replace('"', "'").replace("\\", "\\\\")[:300]
    subprocess.run(
        ["/usr/bin/osascript", "-e", f'display notification "{safe_message}" with title "{title}"'],
        check=True,
        timeout=10,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
