from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_smoke_eval = _load_script("load_smoke_eval")


class FakeLoadApi:
    def __init__(
        self,
        *,
        drop_recall: bool = False,
        metrics: str | None = None,
    ) -> None:
        self._lock = Lock()
        self._items: list[dict[str, Any]] = []
        self._drop_recall = drop_recall
        self._metrics = metrics or "\n".join(
            [
                "uam_outbox_pending_total 0",
                "uam_outbox_lag_seconds 0",
                "uam_outbox_dead_letter_total 0",
            ]
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect_status: int = 200,
        auth: bool = True,
    ) -> Any:
        if method == "GET" and path == "/health":
            return {"status": "ok"}
        if method == "GET" and path == "/metrics":
            return self._metrics
        if method == "POST" and path == "/v1/memory/retain":
            assert expect_status == 201
            with self._lock:
                self._items.append(body or {})
            return {"id": f"mem-{len(self._items)}"}
        if method == "POST" and path == "/v1/memory/recall":
            query = str((body or {}).get("query", ""))
            marker = query.rsplit(" ", 1)[-1]
            if self._drop_recall:
                return {"results": [], "context": {"markdown": ""}}
            with self._lock:
                rows = [
                    item
                    for item in self._items
                    if marker in str(item.get("text", ""))
                ]
            return {
                "results": [{"text": item["text"]} for item in rows],
                "context": {"markdown": "\n".join(str(item["text"]) for item in rows)},
            }
        raise AssertionError(f"unexpected request {method} {path}")


def test_load_smoke_eval_passes_parallel_retain_recall() -> None:
    config = load_smoke_eval.LoadConfig(
        base_url="http://memory.example",
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000002"),
        agents=4,
        operations_per_agent=3,
        run_id="unit",
    )

    report = load_smoke_eval.run_load_smoke(config, FakeLoadApi())

    assert report.ok is True
    assert report.format == "obelisk-load-smoke-v1"
    assert report.total_operations == 12
    assert {check.name for check in report.checks} >= {
        "health",
        "concurrent-retain-recall",
        "error-rate",
        "retain-p95",
        "recall-p95",
        "metrics-backlog",
    }


def test_load_smoke_eval_fails_when_recall_loses_markers() -> None:
    config = load_smoke_eval.LoadConfig(
        base_url="http://memory.example",
        agents=2,
        operations_per_agent=2,
        run_id="lost",
    )

    report = load_smoke_eval.run_load_smoke(config, FakeLoadApi(drop_recall=True))

    assert report.ok is False
    recall = next(check for check in report.checks if check.name == "concurrent-retain-recall")
    assert recall.ok is False
    assert "failed operations" in recall.detail


def test_load_smoke_eval_fails_on_backlog_metrics() -> None:
    config = load_smoke_eval.LoadConfig(
        base_url="http://memory.example",
        agents=1,
        operations_per_agent=1,
        max_outbox_pending=10,
        run_id="backlog",
    )

    report = load_smoke_eval.run_load_smoke(
        config,
        FakeLoadApi(metrics="uam_outbox_pending_total 42\nuam_outbox_lag_seconds 0\n"),
    )

    assert report.ok is False
    metrics = next(check for check in report.checks if check.name == "metrics-backlog")
    assert metrics.ok is False
    assert "outbox pending" in metrics.detail


def test_load_smoke_eval_writes_json_report(tmp_path: Path) -> None:
    report = load_smoke_eval.LoadReport(
        format="obelisk-load-smoke-v1",
        ok=True,
        base_url="http://memory.example",
        tenant_id="00000000-0000-0000-0000-000000000001",
        workspace_id="00000000-0000-0000-0000-000000000002",
        run_id="write",
        agents=1,
        operations_per_agent=1,
        total_operations=1,
        retain_p95_ms=10,
        recall_p95_ms=20,
        error_rate=0.0,
        checks=(load_smoke_eval.CheckResult("health", True, "ok"),),
    )
    path = tmp_path / "ops" / "load-smoke.json"

    load_smoke_eval.write_report(report, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "obelisk-load-smoke-v1"
    assert payload["ok"] is True
