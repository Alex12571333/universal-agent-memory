from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "deploy/observability/grafana-dashboard.json"
ALERTS = ROOT / "deploy/observability/prometheus-alerts.yml"
DOCS = ROOT / "docs/OBSERVABILITY.md"


def test_grafana_dashboard_uses_real_exposed_metrics() -> None:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panel_exprs = {
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    }

    required_metrics = {
        "uam_outbox_pending_total",
        "uam_outbox_dead_letter_total",
        "uam_outbox_lag_seconds",
        "uam_processed_events_inflight_total",
        "uam_embedding_operations_total",
        "uam_embedding_failures_total",
        "uam_embedding_reindex_total",
        "uam_embedding_reindex_failures_total",
        "uam_embedding_last_duration_seconds",
        "uam_embedding_reindex_last_duration_seconds",
        "uam_memory_items_total",
        "uam_audit_events_total",
        "uam_api_keys_total",
        "uam_api_keys_revoked_total",
    }

    all_expressions = "\n".join(sorted(panel_exprs))
    for metric in required_metrics:
        assert metric in all_expressions
    assert dashboard["uid"] == "obelisk-memory-production"
    assert dashboard["refresh"] == "30s"


def test_prometheus_alerts_cover_production_failure_modes() -> None:
    alerts = ALERTS.read_text(encoding="utf-8")

    for alert_name in (
        "ObeliskOutboxDeadLetters",
        "ObeliskOutboxBacklogHigh",
        "ObeliskOutboxLagHigh",
        "ObeliskProcessedEventLeasesHigh",
        "ObeliskEmbeddingFailures",
        "ObeliskReindexFailures",
    ):
        assert f"alert: {alert_name}" in alerts

    for metric in (
        "uam_outbox_dead_letter_total",
        "uam_outbox_pending_total",
        "uam_outbox_lag_seconds",
        "uam_processed_events_inflight_total",
        "uam_embedding_failures_total",
        "uam_embedding_reindex_failures_total",
    ):
        assert metric in alerts


def test_observability_docs_reference_dashboard_alerts_and_release_gate() -> None:
    docs = DOCS.read_text(encoding="utf-8")

    assert "deploy/observability/grafana-dashboard.json" in docs
    assert "deploy/observability/prometheus-alerts.yml" in docs
    assert "scripts/check_metrics_health.py" in docs
    assert "ops/metrics-health.json" in docs
