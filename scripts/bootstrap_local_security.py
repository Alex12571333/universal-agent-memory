#!/usr/bin/env python3
"""Generate scoped local-agent keys and strict bindings in an ignored dotenv file."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

OPENCLAW_AGENT = "00000000-0000-0000-0000-000000000110"
HERMES_AGENT = "00000000-0000-0000-0000-000000000120"
TENANT = "00000000-0000-0000-0000-000000000001"
WORKSPACE = "00000000-0000-0000-0000-000000000002"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", type=Path)
    args = parser.parse_args()
    existing = args.env_file.read_text(encoding="utf-8") if args.env_file.exists() else ""
    if "UAM_API_KEYS=" in existing:
        raise SystemExit(f"{args.env_file} already contains UAM_API_KEYS; refusing to overwrite")
    keys = {name: secrets.token_urlsafe(32) for name in ("openclaw", "hermes", "operator")}
    bindings = {
        "openclaw": {"tenant_id": TENANT, "workspace_id": WORKSPACE, "agent_id": OPENCLAW_AGENT},
        "hermes": {"tenant_id": TENANT, "workspace_id": WORKSPACE, "agent_id": HERMES_AGENT},
    }
    agent_keys = ",".join(
        f"{name}:{key}:agent" for name, key in keys.items() if name != "operator"
    )
    payload = "\n".join(
        [
            "# Generated locally by scripts/bootstrap_local_security.py; never commit.",
            f"UAM_API_KEYS={agent_keys},operator:{keys['operator']}:operator",
            "UAM_API_PRINCIPAL_BINDINGS_JSON=" + json.dumps(bindings, separators=(",", ":")),
            "UAM_REQUIRE_IDENTITY_BINDINGS=true",
            "UAM_UI_SESSION_SIGNING_KEY=" + secrets.token_urlsafe(48),
        ]
    )
    args.env_file.write_text((existing.rstrip() + "\n" + payload + "\n"), encoding="utf-8")
    args.env_file.chmod(0o600)
    print(f"wrote scoped local security configuration to {args.env_file}")
    print("Configure OpenClaw and Hermes with their generated agent keys before restarting Docker.")


if __name__ == "__main__":
    main()
