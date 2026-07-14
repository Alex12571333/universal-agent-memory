"""Verify the local appliance's private NATS and worker dependencies.

The API's public ``/ready`` route intentionally gates only the canonical
PostgreSQL ledger.  This operator-only checker closes the operational gap: it
reads ``/v1/system/status`` and fails a scheduled health check when a required
asynchronous dependency is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from memory_plane.config.secrets import read_secret_env
from memory_plane.services.alerting import send_alert

REPORT_FORMAT = "obelisk-runtime-dependencies-health-v1"
DEFAULT_STATUS_URL = "http://localhost:6798/v1/system/status"
DEFAULT_DEPENDENCIES = ("nats", "embedding_worker")


def _default_api_key() -> str | None:
    """Return a direct key or an operator credential from the scoped keyring."""
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
    """Read dependency status, write a non-secret report and optionally alert."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status-url",
        default=os.getenv("UAM_SYSTEM_STATUS_URL", DEFAULT_STATUS_URL),
    )
    parser.add_argument("--api-key", default=_default_api_key(), help="Operator bearer key")
    parser.add_argument("--report", default=os.getenv("UAM_RUNTIME_DEPENDENCIES_REPORT"))
    parser.add_argument("--alert-webhook", default=read_secret_env("UAM_METRICS_ALERT_WEBHOOK"))
    parser.add_argument("--alert-command", default=os.getenv("UAM_ALERT_COMMAND", ""))
    parser.add_argument(
        "--require-dependency",
        action="append",
        default=[],
        help="Dependency key that must be healthy; defaults to nats and embedding_worker",
    )
    args = parser.parse_args()
    required = tuple(args.require_dependency) or DEFAULT_DEPENDENCIES
    started = time.time()
    try:
        payload = _read_status(args.status_url, args.api_key)
        checks = evaluate_status(payload, required)
    except Exception as exc:  # pragma: no cover - CLI boundary.
        checks = [{"name": "system-status", "ok": False, "detail": type(exc).__name__}]
    report = {
        "format": REPORT_FORMAT,
        "ok": all(row["ok"] for row in checks),
        "checked_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.time() - started, 3),
        "status_url": _safe_url(args.status_url),
        "required_dependencies": list(required),
        "checks": checks,
    }
    if args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    if not report["ok"] and args.alert_webhook:
        send_alert(report, webhook=args.alert_webhook, user_agent="obelisk-runtime-dependencies")
    if not report["ok"] and args.alert_command:
        send_alert(report, command=args.alert_command, user_agent="obelisk-runtime-dependencies")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


def evaluate_status(payload: object, required: tuple[str, ...]) -> list[dict[str, Any]]:
    """Evaluate the non-secret dependency state returned by the operator API."""
    if not isinstance(payload, dict):
        return [{"name": "system-status", "ok": False, "detail": "invalid JSON object"}]
    dependencies = payload.get("runtime_dependencies")
    if not isinstance(dependencies, dict):
        return [{"name": "runtime_dependencies", "ok": False, "detail": "missing"}]
    checks: list[dict[str, Any]] = []
    for name in required:
        row = dependencies.get(name)
        status = row.get("status") if isinstance(row, dict) else None
        checks.append(
            {
                "name": f"dependency:{name}",
                "ok": status == "healthy",
                "detail": "healthy" if status == "healthy" else f"status={status or 'missing'}",
            }
        )
    return checks


def _read_status(url: str, api_key: str | None) -> object:
    headers = {"User-Agent": "obelisk-runtime-dependencies"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - operator input.
        return json.loads(response.read().decode("utf-8"))


def _safe_url(value: str) -> str:
    """Avoid persisting query parameters or credentials in evidence reports."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except ValueError:
        return "<invalid>"


if __name__ == "__main__":
    raise SystemExit(main())
