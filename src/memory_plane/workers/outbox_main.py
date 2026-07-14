"""Executable polling loop for PostgreSQL outbox to NATS JetStream."""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from uuid import UUID

from memory_plane.adapters.nats import NatsJetStreamSink
from memory_plane.adapters.postgres import PostgresMemoryLedger
from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.services.outbox import OutboxRelay
from memory_plane.workers.heartbeat import WorkerHeartbeatEmitter
from memory_plane.workers.logging import log_event


async def run() -> None:
    """Run the standalone relay until its container is stopped."""
    dsn = read_database_dsn()
    if not dsn:
        raise RuntimeError("PostgreSQL connection configuration is required")
    tenant_id = UUID(os.environ["UAM_SERVER_ID"])
    nats_url = os.getenv("UAM_NATS_URL", "nats://nats:4222")
    poll_seconds = float(os.getenv("UAM_OUTBOX_POLL_SECONDS", "0.5"))
    worker_id = os.getenv("UAM_WORKER_ID", socket.gethostname())
    store = PostgresMemoryLedger(dsn)
    sink = NatsJetStreamSink(
        nats_url,
        max_bytes=int(os.getenv("UAM_NATS_STREAM_MAX_BYTES", "536870912")),
        max_age_seconds=int(os.getenv("UAM_NATS_STREAM_MAX_AGE_SECONDS", "604800")),
        auth_token=read_secret_env("UAM_NATS_AUTH_TOKEN"),
    )
    relay = OutboxRelay(
        store,
        sink,
        tenant_id=tenant_id,
        worker_id=worker_id,
        batch_size=int(os.getenv("UAM_OUTBOX_BATCH_SIZE", "50")),
        max_attempts=int(os.getenv("UAM_OUTBOX_MAX_ATTEMPTS", "8")),
        retry_base_seconds=int(os.getenv("UAM_OUTBOX_RETRY_BASE_SECONDS", "5")),
        retry_max_seconds=int(os.getenv("UAM_OUTBOX_RETRY_MAX_SECONDS", "300")),
    )
    heartbeat = WorkerHeartbeatEmitter(
        store,
        tenant_id=tenant_id,
        worker_kind="outbox-relay",
        worker_id=worker_id,
        interval_seconds=float(os.getenv("UAM_WORKER_HEARTBEAT_SECONDS", "5")),
    )
    heartbeat_task: asyncio.Task[None] | None = None
    heartbeat_started = False
    try:
        await sink.connect()
        await heartbeat.start()
        heartbeat_started = True
        heartbeat_task = asyncio.create_task(heartbeat.run())
        log_event("worker_started", worker="outbox-relay")
        while True:
            result = await relay.run_once()
            if heartbeat_task.done():
                heartbeat_task.result()
            if result.claimed == 0:
                await asyncio.sleep(poll_seconds)
    finally:
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
                    worker="outbox-relay",
                    error_type=type(error).__name__,
                )
        log_event("worker_stopped", worker="outbox-relay")
        await sink.close()
        store.close()


def main() -> None:
    """Synchronous console boundary."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
