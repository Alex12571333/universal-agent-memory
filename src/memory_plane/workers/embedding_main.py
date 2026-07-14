"""Executable loop for versioned embedding worker."""

from __future__ import annotations

import asyncio
import os
import socket
import time
from contextlib import suppress
from uuid import UUID

from memory_plane.bootstrap import build_postgres_container
from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.services.consumer import IdempotentEventConsumer
from memory_plane.workers.heartbeat import WorkerHeartbeatEmitter
from memory_plane.workers.logging import log_event
from memory_plane.workers.metrics_server import WorkerMetricsServer
from memory_plane.workers.nats_consumer import NatsPullWorker


async def run() -> None:
    """Run the embedding worker until stopped."""
    dsn = read_database_dsn()
    if not dsn:
        raise RuntimeError("PostgreSQL connection configuration is required")
    server_id = UUID(os.environ["UAM_SERVER_ID"])
    project_id = UUID(os.environ.get("UAM_PROJECT_ID", "00000000-0000-0000-0000-000000000002"))
    nats_url = os.getenv("UAM_NATS_URL", "nats://nats:4222")
    poll_seconds = float(os.getenv("UAM_EMBED_POLL_SECONDS", "0.5"))
    metrics_port = int(os.getenv("UAM_WORKER_METRICS_PORT", "9091"))
    worker_id = os.getenv("UAM_WORKER_ID", socket.gethostname())

    container = _build_container(dsn, server_id=server_id, project_id=project_id)
    started_at = time.time()
    worker_ready = False

    def collect_worker_metrics() -> dict[str, float | int]:
        return {
            **container.embedding.collect_metrics(),
            "embedding_worker_up": int(worker_ready),
            "embedding_worker_start_time_seconds": round(started_at, 6),
        }

    metrics_server = WorkerMetricsServer(collect_worker_metrics)

    async def handler(event: IntegrationEvent) -> None:
        if event.name != "memory.retained.v1":
            return
        jobs = event.payload.get("jobs", [])
        if "embed" not in jobs:
            return

        memory_id_str = event.payload.get("memory_id")
        if not memory_id_str:
            return

        memory_id = UUID(memory_id_str)
        # Run synchronous process_memory_retained in asyncio thread pool to avoid blocking loop
        await asyncio.to_thread(
            container.embedding.process_memory_retained,
            event.tenant_id,
            memory_id,
        )
        log_event(
            "embedding_completed",
            worker="embedding",
            tenant_id=event.tenant_id,
            memory_id=memory_id,
        )

    # Postgres store is both MemoryLedger and ProcessedEventRepository
    consumer = IdempotentEventConsumer(
        container.store,  # type: ignore[arg-type]
        handler,
        consumer="embed-v1",
        worker_id=worker_id,
    )

    worker = NatsPullWorker(
        nats_url,
        consumer,
        durable="EMBEDDING_WORKER",
        subject="memory.events.>",
        stream="MEMORY_EVENTS",
        max_deliveries=int(os.getenv("UAM_NATS_MAX_DELIVERIES", "8")),
        retry_base_seconds=int(os.getenv("UAM_NATS_RETRY_BASE_SECONDS", "2")),
        retry_max_seconds=int(os.getenv("UAM_NATS_RETRY_MAX_SECONDS", "60")),
        dead_letter_stream=os.getenv("UAM_NATS_DLQ_STREAM", "MEMORY_DLQ"),
        dead_letter_subject=os.getenv("UAM_NATS_DLQ_SUBJECT", "memory.dead_letters.embedding"),
        dead_letter_max_bytes=int(os.getenv("UAM_NATS_DLQ_MAX_BYTES", "134217728")),
        dead_letter_max_age_seconds=int(
            os.getenv("UAM_NATS_DLQ_MAX_AGE_SECONDS", "1209600")
        ),
        auth_token=read_secret_env("UAM_NATS_AUTH_TOKEN"),
    )

    heartbeat = WorkerHeartbeatEmitter(
        container.store,  # type: ignore[arg-type]
        tenant_id=server_id,
        worker_kind="embedding-worker",
        worker_id=worker_id,
        interval_seconds=float(os.getenv("UAM_WORKER_HEARTBEAT_SECONDS", "5")),
    )
    heartbeat_task: asyncio.Task[None] | None = None
    heartbeat_started = False
    try:
        await worker.connect()
        await metrics_server.start("0.0.0.0", metrics_port)
        await heartbeat.start()
        heartbeat_started = True
        heartbeat_task = asyncio.create_task(heartbeat.run())
        worker_ready = True
        log_event("worker_started", worker="embedding", metrics_port=metrics_port)
        while True:
            acked = await worker.run_once(batch_size=10, timeout=poll_seconds)
            if heartbeat_task is not None and heartbeat_task.done():
                heartbeat_task.result()
            if acked:
                log_event("worker_batch_completed", worker="embedding", acknowledged=acked)
            if acked == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        worker_ready = False
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
        if heartbeat_started:
            try:
                await heartbeat.stop()
            except Exception as error:  # noqa: BLE001 - still release transport.
                log_event(
                    "worker_heartbeat_stop_failed",
                    worker="embedding",
                    error_type=type(error).__name__,
                )
        log_event("worker_stopped", worker="embedding")
        await metrics_server.close()
        await worker.close()
        container.store.close()


def _build_container(dsn: str, *, server_id: UUID, project_id: UUID):
    """Use the same immutable Qdrant identity as the API process."""
    return build_postgres_container(
        dsn,
        server_id=server_id,
        project_id=project_id,
        qdrant_url=os.getenv("UAM_QDRANT_URL"),
        qdrant_dim=int(os.getenv("UAM_EMBEDDING_DIM", "1536")),
        qdrant_collection=os.getenv("UAM_QDRANT_COLLECTION", "memory_items"),
        require_qdrant=True,
    )


def main() -> None:
    """Console entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
