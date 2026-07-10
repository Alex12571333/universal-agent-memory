"""Verify production deployment boundary before release claims."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from memory_plane.config.secrets import read_secret_env

REPORT_FORMAT = "obelisk-deployment-preflight-v1"
REQUIRED_SECURITY_HEADERS = (
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
)


def main() -> int:
    """Run deployment boundary checks and optionally write JSON evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--public-url",
        required=True,
        help="External HTTPS base URL, for example https://memory.example.com",
    )
    parser.add_argument(
        "--backend-url",
        required=True,
        help="Direct backend URL that must not be externally reachable.",
    )
    parser.add_argument(
        "--api-key",
        default=read_secret_env("UAM_API_KEY"),
        help="Bearer key for authenticated endpoint probes.",
    )
    parser.add_argument("--report", type=Path, help="Write JSON release evidence.")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    report = run_preflight(
        public_url=args.public_url,
        backend_url=args.backend_url,
        api_key=args.api_key,
        timeout=args.timeout,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


def run_preflight(
    *,
    public_url: str,
    backend_url: str,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Return machine-readable deployment boundary evidence."""
    started = time.time()
    public_base = _normalize_base_url(public_url)
    backend_base = _normalize_base_url(backend_url)
    public_health_url = urljoin(public_base, "/health")
    backend_health_url = urljoin(backend_base, "/health")

    checks: list[dict[str, Any]] = []
    public_scheme = urlparse(public_base).scheme
    checks.append(
        {
            "name": "public-url-https",
            "ok": public_scheme == "https",
            "detail": f"scheme={public_scheme}",
        }
    )

    public_response = _probe(public_health_url, api_key=api_key, timeout=timeout)
    checks.append(
        {
            "name": "public-health",
            "ok": public_response["reachable"] and public_response["status"] == 200,
            "detail": public_response["detail"],
        }
    )
    headers = {
        str(key).lower(): str(value)
        for key, value in dict(public_response.get("headers") or {}).items()
    }
    missing_headers = sorted(
        header for header in REQUIRED_SECURITY_HEADERS if header not in headers
    )
    checks.append(
        {
            "name": "public-security-headers",
            "ok": not missing_headers,
            "detail": (
                "required headers present"
                if not missing_headers
                else "missing: " + ", ".join(missing_headers)
            ),
        }
    )

    backend_response = _probe(backend_health_url, api_key=api_key, timeout=timeout)
    checks.append(
        {
            "name": "backend-not-public",
            "ok": not backend_response["reachable"],
            "detail": (
                "direct backend probe failed as expected"
                if not backend_response["reachable"]
                else f"direct backend reachable with status={backend_response['status']}"
            ),
        }
    )

    return {
        "format": REPORT_FORMAT,
        "ok": all(check["ok"] for check in checks),
        "checked_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.time() - started, 3),
        "public_url": public_base,
        "backend_url": backend_base,
        "backend_probe_performed": True,
        "backend_publicly_reachable": bool(backend_response["reachable"]),
        "checks": checks,
    }


def _normalize_base_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value:
        raise ValueError("URL must not be empty")
    return value if value.endswith("/") else value + "/"


def _probe(url: str, *, api_key: str | None, timeout: float) -> dict[str, Any]:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {
                "reachable": True,
                "status": int(response.status),
                "headers": dict(response.headers.items()),
                "detail": f"HTTP {response.status}",
            }
    except urllib.error.HTTPError as exc:
        return {
            "reachable": True,
            "status": int(exc.code),
            "headers": dict(exc.headers.items()),
            "detail": f"HTTP {exc.code}",
        }
    except Exception as exc:  # noqa: BLE001 - boundary probe reports every failure.
        return {
            "reachable": False,
            "status": None,
            "headers": {},
            "detail": f"{type(exc).__name__}: {exc}",
        }


if __name__ == "__main__":
    raise SystemExit(main())
