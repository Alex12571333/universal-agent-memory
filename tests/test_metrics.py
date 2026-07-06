from __future__ import annotations

from memory_plane.services.metrics import render_prometheus


def test_render_prometheus_sorts_and_prefixes_metrics() -> None:
    text = render_prometheus({"outbox_lag_seconds": 2.5, "memory_items_total": 3})

    assert text == (
        "# TYPE uam_memory_items_total counter\n"
        "uam_memory_items_total 3\n"
        "# TYPE uam_outbox_lag_seconds gauge\n"
        "uam_outbox_lag_seconds 2.5\n"
    )
