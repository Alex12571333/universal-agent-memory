from __future__ import annotations

import json

from memory_plane.workers.logging import log_event


def test_log_event_emits_one_json_line_with_timestamp(capsys) -> None:
    log_event("worker_started", worker="embedding", metrics_port=9091)

    payload = json.loads(capsys.readouterr().out)
    assert payload["event"] == "worker_started"
    assert payload["worker"] == "embedding"
    assert payload["metrics_port"] == 9091
    assert payload["timestamp"].endswith("+00:00")
