"""Run one authenticated bounded purge of expired curated-only transcript staging."""

from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from memory_plane.config.secrets import read_secret_env


def _default_api_key() -> str | None:
    """Resolve a direct/file secret or an operator key from the scoped keyring."""
    direct = read_secret_env("UAM_API_KEY")
    if direct:
        return direct
    for entry in (read_secret_env("UAM_API_KEYS") or "").split(","):
        try:
            _name, secret, scopes = entry.strip().split(":", 2)
        except ValueError:
            continue
        allowed = {part.strip().lower() for part in scopes.replace("|", "+").split("+")}
        if "operator" in allowed and secret.strip():
            return secret.strip()
    return None


def main() -> int:
    """Invoke the operator-only retention endpoint and emit its JSON result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--api-key",
        default=_default_api_key(),
        help="Operator bearer key; defaults to UAM_API_KEY[_FILE] or UAM_API_KEYS[_FILE]",
    )
    parser.add_argument("--tenant-id", required=True, type=UUID)
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if not args.api_key:
        parser.error("operator API key is required")
    limit = max(1, min(args.limit, 5000))
    url = (
        f"{args.base_url.rstrip('/')}/v1/workspaces/{args.workspace_id}"
        f"/conversations/purge-expired?tenant_id={args.tenant_id}&limit={limit}"
    )
    request = Request(
        url,
        data=b"",
        headers={"Authorization": f"Bearer {args.api_key}"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **payload}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
