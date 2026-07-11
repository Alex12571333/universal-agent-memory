from __future__ import annotations

from memory_plane.workers.metrics_server import WorkerMetricsServer


def test_worker_metrics_endpoint_renders_worker_counters() -> None:
    server = WorkerMetricsServer(
        lambda: {"embedding_worker_up": 1, "embedding_operations_total": 4}
    )

    response = server.response("GET", "/metrics").decode()

    assert response.startswith("HTTP/1.1 200 OK")
    assert "# TYPE uam_embedding_worker_up gauge" in response
    assert "uam_embedding_worker_up 1" in response
    assert "uam_embedding_operations_total 4" in response


def test_worker_metrics_endpoint_has_liveness_and_rejects_unknown_routes() -> None:
    server = WorkerMetricsServer(lambda: {})

    assert "\r\n\r\nok\n" in server.response("GET", "/healthz").decode()
    assert server.response("POST", "/metrics").startswith(b"HTTP/1.1 405")
    assert server.response("GET", "/missing").startswith(b"HTTP/1.1 404")
