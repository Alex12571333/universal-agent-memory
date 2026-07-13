"""Prove the local advanced memory pipeline without exposing retained text.

The probe is intended for an operator-run Docker appliance.  It writes one
unique synthetic marker, waits for asynchronous embedding, and requires the
marker to return through the Qdrant hybrid source.  Its optional report is
redacted: it contains only IDs, source names, dependency states and timings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:6798")
    parser.add_argument(
        "--api-key-env",
        default="UAM_API_KEY",
        help="environment variable or *_FILE pair holding an operator credential",
    )
    parser.add_argument("--timeout-seconds", type=float, default=45)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    api_key = _read_secret(args.api_key_env)
    if not api_key:
        raise SystemExit(
            f"missing operator credential in {args.api_key_env} or {args.api_key_env}_FILE"
        )
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")

    started = time.monotonic()
    marker = f"obelisk-advanced-pipeline-{secrets.token_hex(12)}"
    result: dict[str, Any] = {
        "format": "obelisk-advanced-pipeline-probe-v1",
        "ok": False,
        "started_at": datetime.now(UTC).isoformat(),
        "marker_sha256": hashlib.sha256(marker.encode()).hexdigest(),
    }
    try:
        status = _request(args.base_url, api_key, "GET", "/v1/system/status")
        dependencies = status.get("runtime_dependencies", {})
        result["dependencies"] = dependencies
        if not _dependency_is_healthy(dependencies, "nats") or not _dependency_is_healthy(
            dependencies, "embedding_worker"
        ):
            raise RuntimeError("advanced runtime dependencies are not healthy")

        retained = _request(
            args.base_url,
            api_key,
            "POST",
            "/v1/memory/retain",
            {
                "layer": "semantic",
                "scope": "workspace",
                "kind": "runtime_probe",
                "text": marker,
                "source_kind": "advanced_pipeline_probe",
                "labels": ["runtime-probe"],
                "importance": 0.01,
                "confidence": 1.0,
                "idempotency_key": f"advanced-pipeline:{marker}",
            },
        )
        result["memory_id"] = retained.get("id")

        deadline = time.monotonic() + args.timeout_seconds
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            recalled = _request(
                args.base_url,
                api_key,
                "POST",
                "/v1/memory/recall",
                {"query": marker, "top_k": 10, "context_budget_tokens": 128},
            )
            sources = list(recalled.get("sources_used", []))
            result["sources_used"] = sources
            found = any(row.get("id") == result["memory_id"] for row in recalled.get("results", []))
            # These are intentionally booleans only: they make a failed
            # release probe diagnosable without retaining marker/query text.
            result["marker_found"] = found
            result["index_stale"] = recalled.get("index_stale")
            if found and "qdrant_hybrid" in sources and recalled.get("index_stale") is False:
                result["attempts"] = attempts
                result["elapsed_seconds"] = round(time.monotonic() - started, 3)
                result["ok"] = True
                break
            time.sleep(1)
        if not result["ok"]:
            raise RuntimeError("marker did not return through qdrant_hybrid before timeout")
        return 0
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        result["completed_at"] = datetime.now(UTC).isoformat()
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        print(json.dumps(result, sort_keys=True))


def _read_secret(name: str) -> str | None:
    direct = os.getenv(name)
    if direct:
        return direct
    file_name = os.getenv(f"{name}_FILE")
    if not file_name:
        return None
    value = Path(file_name).read_text(encoding="utf-8").strip()
    return value or None


def _dependency_is_healthy(dependencies: object, name: str) -> bool:
    if not isinstance(dependencies, dict):
        return False
    dependency = dependencies.get(name)
    return isinstance(dependency, dict) and dependency.get("status") == "healthy"


def _request(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - explicit operator URL.
            return json.loads(response.read())
    except HTTPError as exc:
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
