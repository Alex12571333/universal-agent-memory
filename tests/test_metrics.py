from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from memory_plane.services.metrics import render_prometheus

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_metrics_health = _load_script("check_metrics_health")


def test_render_prometheus_sorts_and_prefixes_metrics() -> None:
    text = render_prometheus({"outbox_lag_seconds": 2.5, "memory_items_total": 3})

    assert text == (
        "# TYPE uam_memory_items_total counter\n"
        "uam_memory_items_total 3\n"
        "# TYPE uam_outbox_lag_seconds gauge\n"
        "uam_outbox_lag_seconds 2.5\n"
    )


def test_render_prometheus_marks_duration_sums_as_counters() -> None:
    text = render_prometheus({"embedding_duration_seconds_sum": 2.5, "worker_up": 1})

    assert "# TYPE uam_embedding_duration_seconds_sum counter" in text
    assert "# TYPE uam_worker_up gauge" in text


def test_metrics_health_passes_when_outbox_thresholds_are_green() -> None:
    metrics = check_metrics_health.parse_prometheus_metrics(
        """
        # TYPE uam_outbox_pending_total counter
        uam_outbox_pending_total 3
        uam_outbox_dead_letter_total 0
        uam_outbox_lag_seconds 12
        uam_processed_events_inflight_total 1
        """
    )

    checks = check_metrics_health.evaluate_metrics(
        metrics,
        max_outbox_pending=10,
        max_outbox_dead_letter=0,
        max_outbox_lag_seconds=60,
        max_inflight=5,
        required_metrics=("uam_outbox_pending_total",),
    )

    assert all(check["ok"] for check in checks)


def test_metrics_health_fails_on_dead_letters_lag_and_missing_metric() -> None:
    metrics = check_metrics_health.parse_prometheus_metrics(
        """
        uam_outbox_pending_total 2
        uam_outbox_dead_letter_total 1
        uam_outbox_lag_seconds 900
        uam_processed_events_inflight_total 0
        """
    )

    checks = check_metrics_health.evaluate_metrics(
        metrics,
        max_outbox_pending=10,
        max_outbox_dead_letter=0,
        max_outbox_lag_seconds=60,
        max_inflight=5,
        required_metrics=("embedding_latency_seconds",),
    )

    failed = {check["name"] for check in checks if not check["ok"]}
    assert failed == {
        "outbox_dead_letter_total",
        "outbox_lag_seconds",
        "required:embedding_latency_seconds",
    }


def test_metrics_health_cli_writes_report_and_alerts_on_failure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    metrics_file = tmp_path / "metrics.prom"
    report = tmp_path / "metrics-report.json"
    metrics_file.write_text(
        "\n".join(
            [
                "uam_outbox_pending_total 0",
                "uam_outbox_dead_letter_total 2",
                "uam_outbox_lag_seconds 10",
                "uam_processed_events_inflight_total 0",
            ]
        ),
        encoding="utf-8",
    )
    alerts: list[dict[str, object]] = []

    def fake_send_alert(_webhook: str, payload: dict[str, object]) -> None:
        alerts.append(payload)

    monkeypatch.setattr(check_metrics_health, "_send_alert", fake_send_alert)
    monkeypatch.setattr(
        "sys.argv",
        [
            "check_metrics_health.py",
            "--metrics-file",
            str(metrics_file),
            "--report",
            str(report),
            "--alert-webhook",
            "https://alerts.example/metrics",
        ],
    )

    assert check_metrics_health.main() == 1

    payload = json.loads(report.read_text(encoding="utf-8"))
    stdout = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert stdout["format"] == "obelisk-metrics-health-v1"
    assert alerts and alerts[0]["ok"] is False
