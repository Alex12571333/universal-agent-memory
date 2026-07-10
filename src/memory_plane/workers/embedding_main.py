"""Executable loop for versioned embedding worker."""

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
    """Run the embedding worker until stopped."""
    dsn = read_database_dsn()
    if not dsn:
        raise RuntimeError("PostgreSQL connection configuration is required")
    server_id = UUID(os.environ["UAM_SERVER_ID"])
    project_id = UUID(os.environ.get("UAM_PROJECT_ID", "00000000-0000-0000-0000-000000000002"))
    nats_url = os.getenv("UAM_NATS_URL", "nats://nats:4222")
    poll_seconds = float(os.getenv("UAM_EMBED_POLL_SECONDS", "0.5"))

    # Build container to get PostgresLedger and EmbeddingService
    container = build_postgres_container(dsn, server_id=server_id, project_id=project_id)

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

    # Postgres store is both MemoryLedger and ProcessedEventRepository
    consumer = IdempotentEventConsumer(
        container.store,  # type: ignore[arg-type]
        handler,
        consumer="embed-v1",
        worker_id=os.getenv("UAM_WORKER_ID", socket.gethostname()),
    )

    worker = NatsPullWorker(
        nats_url,
        consumer,
        durable="EMBEDDING_WORKER",
        subject="memory.events.>",
        stream="MEMORY_EVENTS",
    )

    await worker.connect()
    try:
        while True:
            acked = await worker.run_once(batch_size=10, timeout=poll_seconds)
            if acked == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        await worker.close()


def main() -> None:
    """Console entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
