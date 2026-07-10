"""Secret environment helpers.

Production deployments should prefer secret files mounted from Docker secrets,
Kubernetes secrets, Vault Agent, SOPS, or another external secret manager.
Direct environment variables remain supported for local development and
backward compatibility.
"""

from __future__ import annotations

import os
from pathlib import Path


def read_secret_env(name: str, *fallback_names: str) -> str | None:
    """Read a secret from `NAME` or `NAME_FILE`.

    Resolution order is intentionally compatibility-first:

    1. non-empty direct environment variable;
    2. non-empty file referenced by `<NAME>_FILE`;
    3. the same lookup for fallback names in order.

    The returned value is stripped of trailing newlines common in mounted secret
    files. Empty files are treated as unset.
    """
    for key in (name, *fallback_names):
        value = os.getenv(key)
        if value:
            return value
        file_name = os.getenv(f"{key}_FILE")
        if not file_name:
            continue
        secret = Path(file_name).read_text(encoding="utf-8").strip()
        if secret:
            return secret
    return None
