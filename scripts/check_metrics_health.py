"""Evaluate Obelisk Memory Prometheus metrics against production thresholds."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from memory_plane.config.secrets import read_secret_env
from memory_plane.services.alerting import send_alert

DEFAULT_URL = "http://localhost:6798/metrics"


def _default_api_key() -> str | None:
    """Return a direct key or the local operator key from the scoped keyring."""
    direct = read_secret_env("UAM_API_KEY")
    if direct:
        return direct
    for entry in (read_secret_env("UAM_API_KEYS") or "").split(","):
        try:
            _name, secret, scopes = entry.strip().split(":", 2)
        except ValueError:
            continue
        if "operator" in {
            value.strip().lower() for value in scopes.replace("|", "+").split("+")
        } and secret.strip():
            return secret.strip()
    return None


def main() -> int:
    """Read metrics, evaluate thresholds, write report and optionally alert."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics-url", default=os.getenv("UAM_METRICS_URL", DEFAULT_URL))
    parser.add_argument("--metrics-file", help="Read Prometheus text from a local file")
    parser.add_argument(
        "--api-key",
        default=_default_api_key(),
        help="Bearer key for /metrics",
    )
    parser.add_argument("--report", default=os.getenv("UAM_METRICS_REPORT"))
    parser.add_argument("--alert-webhook", default=read_secret_env("UAM_METRICS_ALERT_WEBHOOK"))
    parser.add_argument("--alert-command", default=os.getenv("UAM_ALERT_COMMAND", ""))
    parser.add_argument("--max-outbox-pending", type=float, default=100)
    parser.add_argument("--max-outbox-dead-letter", type=float, default=0)
    parser.add_argument("--max-outbox-lag-seconds", type=float, default=300)
    parser.add_argument("--max-inflight", type=float, default=100)
    parser.add_argument(
        "--require-metric",
        action="append",
        default=[],
        help="Metric name that must exist, with or without uam_ prefix",
    )
    args = parser.parse_args()

    started = time.time()
    try:
        text = _read_metrics(args.metrics_file, args.metrics_url, args.api_key)
        metrics = parse_prometheus_metrics(text)
        checks = evaluate_metrics(
            metrics,
            max_outbox_pending=args.max_outbox_pending,
            max_outbox_dead_letter=args.max_outbox_dead_letter,
            max_outbox_lag_seconds=args.max_outbox_lag_seconds,
            max_inflight=args.max_inflight,
            required_metrics=tuple(args.require_metric),
        )
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        metrics = {}
        checks = [
            {
                "name": "metrics-read",
                "ok": False,
                "detail": str(exc),
                "value": None,
                "threshold": None,
            }
        ]

    ok = all(check["ok"] for check in checks)
    report = {
        "format": "obelisk-metrics-health-v1",
        "ok": ok,
        "checked_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.time() - started, 3),
        "metrics_url": None if args.metrics_file else args.metrics_url,
        "metrics_file": args.metrics_file,
        "checks": checks,
        "observed": {
            key: metrics[key]
            for key in sorted(metrics)
            if key.startswith("outbox_")
            or key.startswith("processed_events_")
            or key in {"memory_items_total", "audit_events_total"}
        },
    }
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    if not ok and args.alert_webhook:
        _send_alert(args.alert_webhook, report)
    if not ok and args.alert_command:
        send_alert(report, command=args.alert_command, user_agent="obelisk-memory-metrics-health")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if ok else 1


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    """Parse simple Prometheus text exposition into unprefixed metric names."""
    metrics: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0].split("{", 1)[0]
        if name.startswith("uam_"):
            name = name.removeprefix("uam_")
        try:
            metrics[name] = float(parts[1])
        except ValueError:
            continue
    return metrics


def evaluate_metrics(
    metrics: dict[str, float],
    *,
    max_outbox_pending: float,
    max_outbox_dead_letter: float,
    max_outbox_lag_seconds: float,
    max_inflight: float,
    required_metrics: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Evaluate production health checks from parsed metric values."""
    checks = [
        _lte_check(metrics, "outbox_pending_total", max_outbox_pending),
        _lte_check(metrics, "outbox_dead_letter_total", max_outbox_dead_letter),
        _lte_check(metrics, "outbox_lag_seconds", max_outbox_lag_seconds),
        _lte_check(metrics, "processed_events_inflight_total", max_inflight),
    ]
    for metric in required_metrics:
        normalized = metric.removeprefix("uam_")
        checks.append(
            {
                "name": f"required:{normalized}",
                "ok": normalized in metrics,
                "detail": "metric present" if normalized in metrics else "metric missing",
                "value": metrics.get(normalized),
                "threshold": "present",
            }
        )
    return checks


def _lte_check(metrics: dict[str, float], name: str, threshold: float) -> dict[str, Any]:
    value = metrics.get(name)
    ok = value is not None and value <= threshold
    return {
        "name": name,
        "ok": ok,
        "detail": (
            f"{value} <= {threshold}"
            if value is not None
            else "metric missing"
        ),
        "value": value,
        "threshold": threshold,
    }


def _read_metrics(metrics_file: str | None, metrics_url: str, api_key: str | None) -> str:
    if metrics_file:
        if metrics_file == "-":
            return sys.stdin.read()
        return Path(metrics_file).read_text(encoding="utf-8")
    headers = {"User-Agent": "obelisk-memory-metrics-health"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(metrics_url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8")


def _send_alert(webhook: str, report: dict[str, Any]) -> None:
    """Compatibility wrapper for the webhook transport."""
    send_alert(report, webhook=webhook, user_agent="obelisk-memory-metrics-health")


if __name__ == "__main__":
    raise SystemExit(main())
