"""Best-effort alert delivery for local or webhook-based Obelisk operations."""

from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.request
from typing import Any


def send_alert(
    report: dict[str, Any], *, webhook: str | None = None,
    command: str | None = None, user_agent: str,
) -> bool:
    """Deliver a redacted operational report to at least one configured route."""
    delivered = False
    payload = json.dumps(report, ensure_ascii=False).encode("utf-8")
    if webhook:
        request = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json", "User-Agent": user_agent},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10).close()
            delivered = True
        except urllib.error.URLError:
            pass
    if command:
        try:
            subprocess.run(shlex.split(command), input=payload, check=True, timeout=15)
            delivered = True
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return delivered
