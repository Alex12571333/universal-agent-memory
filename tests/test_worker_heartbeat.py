from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memory_plane.adapters.in_memory import InMemoryMemoryStore
from memory_plane.api.app import DEFAULT_SERVER_ID, create_app
from memory_plane.bootstrap import build_in_memory_container
from memory_plane.domain.worker import WorkerHeartbeat
from memory_plane.workers.heartbeat import WorkerHeartbeatEmitter


def heartbeat(
    *,
    tenant_id=DEFAULT_SERVER_ID,  # type: ignore[no-untyped-def]
    worker_kind: str = "embedding-worker",
    worker_id: str = "worker-1",
    last_seen_at: datetime | None = None,
    status: str = "running",
) -> WorkerHeartbeat:
    now = datetime.now(UTC)
    return WorkerHeartbeat(
        tenant_id=tenant_id,
        worker_kind=worker_kind,
        worker_id=worker_id,
        started_at=now - timedelta(minutes=5),
        last_seen_at=last_seen_at or now,
        status=status,
    )


def test_worker_heartbeat_domain_rejects_unsafe_identity_and_state() -> None:
    with pytest.raises(ValueError, match="worker_kind"):
        heartbeat(worker_kind="")
    with pytest.raises(ValueError, match="worker_id"):
        heartbeat(worker_id="x" * 129)
    with pytest.raises(ValueError, match="status"):
        heartbeat(status="unknown")


def test_in_memory_worker_readiness_distinguishes_fresh_stale_and_missing() -> None:
    store = InMemoryMemoryStore()
    store.record_worker_heartbeat(heartbeat(worker_kind="outbox-relay"))
    store.record_worker_heartbeat(
        heartbeat(
            worker_kind="embedding-worker",
            last_seen_at=datetime.now(UTC) - timedelta(minutes=2),
        )
    )
    store.record_worker_heartbeat(
        heartbeat(worker_kind="embedding-worker", worker_id="worker-2", status="stopping")
    )

    snapshot = store.worker_readiness(
        DEFAULT_SERVER_ID,
        ("outbox-relay", "embedding-worker", "maintenance-worker"),
        stale_after_seconds=30,
    )

    assert snapshot.ready is False
    assert snapshot.missing_kinds == ("maintenance-worker",)
    assert snapshot.stale_kinds == ("embedding-worker",)
    by_kind = {row.worker_kind: row for row in snapshot.required}
    assert by_kind["outbox-relay"].fresh_instances == 1
    assert by_kind["embedding-worker"].stale_instances == 2


def test_worker_heartbeat_emitter_throttles_ticks_and_marks_stop(monkeypatch) -> None:
    store = InMemoryMemoryStore()
    monotonic = [100.0]
    monkeypatch.setattr(
        "memory_plane.workers.heartbeat.time.monotonic",
        lambda: monotonic[0],
    )
    emitter = WorkerHeartbeatEmitter(
        store,
        tenant_id=DEFAULT_SERVER_ID,
        worker_kind="outbox-relay",
        worker_id="relay-1",
        interval_seconds=5,
    )

    async def scenario() -> tuple[bool, bool]:
        await emitter.start()
        skipped = await emitter.tick()
        monotonic[0] = 106.0
        written = await emitter.tick()
        await emitter.stop()
        return skipped, written

    skipped, written = asyncio.run(scenario())
    row = store._worker_heartbeats[(DEFAULT_SERVER_ID, "outbox-relay", "relay-1")]

    assert skipped is False
    assert written is True
    assert row.status == "stopping"


def test_worker_heartbeat_background_loop_runs_during_other_async_work() -> None:
    recorded: list[WorkerHeartbeat] = []

    class Repository:
        def record_worker_heartbeat(self, row: WorkerHeartbeat) -> WorkerHeartbeat:
            recorded.append(row)
            return row

    emitter = WorkerHeartbeatEmitter(
        Repository(),  # type: ignore[arg-type]
        tenant_id=DEFAULT_SERVER_ID,
        worker_kind="embedding-worker",
        worker_id="embed-1",
        interval_seconds=0.5,
    )

    async def scenario() -> int:
        await emitter.start()
        task = asyncio.create_task(emitter.run())
        await asyncio.sleep(0.55)
        running_count = len(recorded)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await emitter.stop()
        return running_count

    assert asyncio.run(scenario()) >= 2
    assert recorded[-1].status == "stopping"


def test_ready_fails_closed_for_missing_required_workers_and_recovers(monkeypatch) -> None:
    monkeypatch.setenv(
        "UAM_REQUIRED_WORKERS",
        "outbox-relay,embedding-worker,maintenance-worker",
    )
    monkeypatch.setenv("UAM_WORKER_HEARTBEAT_TTL_SECONDS", "30")
    container = build_in_memory_container()
    client = TestClient(create_app(container))

    missing = client.get("/ready")
    for kind in ("outbox-relay", "embedding-worker", "maintenance-worker"):
        container.store.record_worker_heartbeat(
            heartbeat(worker_kind=kind, worker_id=f"private-hostname-{kind}")
        )
    ready = client.get("/ready")

    assert missing.status_code == 503
    assert missing.json()["status"] == "not_ready"
    assert missing.json()["worker_pipeline"]["missing"] == [
        "outbox-relay",
        "embedding-worker",
        "maintenance-worker",
    ]
    assert ready.status_code == 200
    assert ready.json()["worker_pipeline"]["status"] == "ready"
    assert ready.json()["worker_pipeline"]["ready_count"] == 3
    assert "private-hostname" not in ready.text


def test_ready_reports_stopping_worker_and_metrics_without_identity_leak(monkeypatch) -> None:
    monkeypatch.setenv("UAM_REQUIRED_WORKERS", "outbox-relay,embedding-worker")
    container = build_in_memory_container()
    container.store.record_worker_heartbeat(
        heartbeat(worker_kind="outbox-relay", worker_id="relay-private-id")
    )
    container.store.record_worker_heartbeat(
        heartbeat(
            worker_kind="embedding-worker",
            worker_id="embed-private-id",
            status="stopping",
        )
    )
    client = TestClient(create_app(container, api_key="operator-secret"))

    response = client.get("/ready")
    metrics = client.get(
        "/metrics",
        headers={"Authorization": "Bearer operator-secret"},
    )

    assert response.status_code == 503
    assert response.json()["worker_pipeline"]["stale"] == ["embedding-worker"]
    assert "private-id" not in response.text
    assert "uam_worker_required 2" in metrics.text
    assert "uam_worker_ready 1" in metrics.text
    assert "uam_worker_unready 1" in metrics.text
    assert "uam_worker_missing 0" in metrics.text
    assert "uam_worker_stale 1" in metrics.text
    assert "private-id" not in metrics.text


def test_worker_readiness_configuration_is_validated(monkeypatch) -> None:
    monkeypatch.setenv("UAM_REQUIRED_WORKERS", "bad worker")
    with pytest.raises(ValueError, match="invalid worker kind"):
        create_app(build_in_memory_container())

    monkeypatch.setenv("UAM_REQUIRED_WORKERS", "embedding-worker")
    monkeypatch.setenv("UAM_WORKER_HEARTBEAT_TTL_SECONDS", "4")
    with pytest.raises(ValueError, match="between 5 and 600"):
        create_app(build_in_memory_container())


def test_worker_readiness_is_tenant_scoped() -> None:
    store = InMemoryMemoryStore()
    foreign_tenant = uuid4()
    store.record_worker_heartbeat(
        heartbeat(tenant_id=foreign_tenant, worker_kind="embedding-worker")
    )

    snapshot = store.worker_readiness(
        DEFAULT_SERVER_ID,
        ("embedding-worker",),
        stale_after_seconds=30,
    )

    assert snapshot.ready is False
    assert snapshot.missing_kinds == ("embedding-worker",)
