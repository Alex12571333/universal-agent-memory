"""Verify installed observability dashboard and alert-rule artifacts."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_FORMAT = "obelisk-observability-preflight-v1"
REQUIRED_METRICS = (
    "uam_outbox_pending_total",
    "uam_outbox_dead_letter_total",
    "uam_outbox_lag_seconds",
    "uam_processed_events_inflight_total",
    "uam_embedding_failures_total",
    "uam_embedding_reindex_failures_total",
)
REQUIRED_ALERTS = (
    "ObeliskOutboxDeadLetters",
    "ObeliskOutboxBacklogHigh",
    "ObeliskOutboxLagHigh",
    "ObeliskProcessedEventLeasesHigh",
    "ObeliskEmbeddingFailures",
    "ObeliskReindexFailures",
)


def main() -> int:
    """Check dashboard/alert artifacts and optionally write release evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grafana-dashboard", type=Path, required=True)
    parser.add_argument("--prometheus-alerts", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="Write JSON release evidence.")
    args = parser.parse_args()

    report = run_preflight(
        grafana_dashboard=args.grafana_dashboard,
        prometheus_alerts=args.prometheus_alerts,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


def run_preflight(*, grafana_dashboard: Path, prometheus_alerts: Path) -> dict[str, Any]:
    """Return machine-readable observability installation evidence."""
    dashboard_text = _read_text(grafana_dashboard)
    alerts_text = _read_text(prometheus_alerts)
    checks: list[dict[str, Any]] = [
        _json_check("grafana-dashboard:json-valid", dashboard_text),
        _contains_all_check(
            "grafana-dashboard:required-metrics",
            dashboard_text,
            REQUIRED_METRICS,
        ),
        _contains_all_check(
            "prometheus-alerts:required-alerts",
            alerts_text,
            REQUIRED_ALERTS,
        ),
        _contains_all_check(
            "prometheus-alerts:required-metrics",
            alerts_text,
            REQUIRED_METRICS,
        ),
    ]
    checks.append(_prometheus_group_check(alerts_text))
    return {
        "format": REPORT_FORMAT,
        "ok": all(check["ok"] for check in checks),
        "checked_at": datetime.now(UTC).isoformat(),
        "grafana_dashboard": str(grafana_dashboard),
        "prometheus_alerts": str(prometheus_alerts),
        "required_metrics": list(REQUIRED_METRICS),
        "required_alerts": list(REQUIRED_ALERTS),
        "checks": checks,
    }


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_check(name: str, text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"name": name, "ok": False, "detail": f"invalid JSON: {exc}"}
    ok = isinstance(payload, dict) and bool(payload.get("panels"))
    return {
        "name": name,
        "ok": ok,
        "detail": "dashboard JSON has panels" if ok else "dashboard JSON missing panels",
    }


def _contains_all_check(name: str, text: str, needles: tuple[str, ...]) -> dict[str, Any]:
    missing = sorted(needle for needle in needles if needle not in text)
    return {
        "name": name,
        "ok": not missing,
        "detail": (
            "all required entries present" if not missing else "missing: " + ", ".join(missing)
        ),
    }


def _prometheus_group_check(text: str) -> dict[str, Any]:
    ok = bool(re.search(r"(?m)^\s*-\s*name:\s*obelisk-memory-production\s*$", text))
    return {
        "name": "prometheus-alerts:production-group",
        "ok": ok,
        "detail": (
            "obelisk-memory-production group present"
            if ok
            else "obelisk-memory-production group missing"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
