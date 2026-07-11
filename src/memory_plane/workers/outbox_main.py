"""Executable polling loop for PostgreSQL outbox to NATS JetStream."""

from __future__ import annotations

import asyncio
import os
import socket
from uuid import UUID

from memory_plane.adapters.nats import NatsJetStreamSink
from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.config.database import read_database_dsn
from memory_plane.services.outbox import OutboxRelay


async def run() -> None:
    """Run the standalone relay until its container is stopped."""
    dsn = read_database_dsn()
    if not dsn:
        raise RuntimeError("PostgreSQL connection configuration is required")
    tenant_id = UUID(os.environ["UAM_SERVER_ID"])
    nats_url = os.getenv("UAM_NATS_URL", "nats://nats:4222")
    poll_seconds = float(os.getenv("UAM_OUTBOX_POLL_SECONDS", "0.5"))
    store = PostgresMemoryLedger(dsn)
    sink = NatsJetStreamSink(
        nats_url,
        max_bytes=int(os.getenv("UAM_NATS_STREAM_MAX_BYTES", "536870912")),
        max_age_seconds=int(os.getenv("UAM_NATS_STREAM_MAX_AGE_SECONDS", "604800")),
    )
    await sink.connect()
    relay = OutboxRelay(
        store,
        sink,
        tenant_id=tenant_id,
        worker_id=os.getenv("UAM_WORKER_ID", socket.gethostname()),
        batch_size=int(os.getenv("UAM_OUTBOX_BATCH_SIZE", "50")),
        max_attempts=int(os.getenv("UAM_OUTBOX_MAX_ATTEMPTS", "8")),
        retry_base_seconds=int(os.getenv("UAM_OUTBOX_RETRY_BASE_SECONDS", "5")),
        retry_max_seconds=int(os.getenv("UAM_OUTBOX_RETRY_MAX_SECONDS", "300")),
    )
    try:
        while True:
            result = await relay.run_once()
            if result.claimed == 0:
                await asyncio.sleep(poll_seconds)
    finally:
        await sink.close()


def main() -> None:
    """Synchronous console boundary."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
