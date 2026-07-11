"""Asynchronous deterministic reflection worker."""

from __future__ import annotations

import asyncio
import os
import socket
from uuid import UUID

from memory_plane.bootstrap import build_postgres_container
from memory_plane.config.database import read_database_dsn
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.services.consumer import IdempotentEventConsumer
from memory_plane.workers.nats_consumer import NatsPullWorker


async def run() -> None:
    """Process reflection jobs until stopped."""
    dsn = read_database_dsn()
    if not dsn:
        raise RuntimeError("PostgreSQL connection configuration is required")
    container = build_postgres_container(
        dsn,
        server_id=UUID(os.environ["UAM_SERVER_ID"]),
        project_id=UUID(os.environ.get("UAM_PROJECT_ID", "00000000-0000-0000-0000-000000000002")),
    )

    async def handler(event: IntegrationEvent) -> None:
        if event.name == "memory.retained.v1" and "reflect" in event.payload.get("jobs", []):
            await asyncio.to_thread(
                container.reflection.reflect,
                event.tenant_id,
                event.workspace_id,
            )

    consumer = IdempotentEventConsumer(
        container.store,  # type: ignore[arg-type]
        handler,
        consumer="maintenance-reflect-v1",
        worker_id=os.getenv("UAM_WORKER_ID", socket.gethostname()),
    )
    worker = NatsPullWorker(
        os.getenv("UAM_NATS_URL", "nats://nats:4222"),
        consumer,
        durable="MAINTENANCE_REFLECT_WORKER",
        subject="memory.events.>",
        stream="MEMORY_EVENTS",
        max_deliveries=int(os.getenv("UAM_NATS_MAX_DELIVERIES", "8")),
        retry_base_seconds=int(os.getenv("UAM_NATS_RETRY_BASE_SECONDS", "2")),
        retry_max_seconds=int(os.getenv("UAM_NATS_RETRY_MAX_SECONDS", "60")),
        dead_letter_stream=os.getenv("UAM_NATS_DLQ_STREAM", "MEMORY_DLQ"),
        dead_letter_subject=os.getenv("UAM_NATS_DLQ_SUBJECT", "memory.dead_letters.maintenance"),
    )
    await worker.connect()
    try:
        while True:
            if await worker.run_once(batch_size=10, timeout=0.5) == 0:
                await asyncio.sleep(0.5)
    finally:
        await worker.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
