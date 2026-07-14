"""Asynchronous deterministic reflection worker."""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from uuid import UUID

from memory_plane.bootstrap import build_postgres_container
from memory_plane.config.database import read_database_dsn
from memory_plane.config.secrets import read_secret_env
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.services.consumer import IdempotentEventConsumer
from memory_plane.services.conversations import CurateConversationTurnCommand
from memory_plane.workers.heartbeat import WorkerHeartbeatEmitter
from memory_plane.workers.logging import log_event
from memory_plane.workers.nats_consumer import NatsPullWorker


async def run() -> None:
    """Process reflection and safe conversation-curation jobs until stopped."""
    dsn = read_database_dsn()
    if not dsn:
        raise RuntimeError("PostgreSQL connection configuration is required")
    server_id = UUID(os.environ["UAM_SERVER_ID"])
    worker_id = os.getenv("UAM_WORKER_ID", socket.gethostname())
    container = build_postgres_container(
        dsn,
        server_id=server_id,
        project_id=UUID(os.environ.get("UAM_PROJECT_ID", "00000000-0000-0000-0000-000000000002")),
    )

    async def handler(event: IntegrationEvent) -> None:
        if event.name == "memory.retained.v1" and "reflect" in event.payload.get("jobs", []):
            await asyncio.to_thread(
                container.reflection.reflect,
                event.tenant_id,
                event.workspace_id,
            )
            log_event(
                "reflection_completed",
                worker="maintenance",
                tenant_id=event.tenant_id,
                workspace_id=event.workspace_id,
            )
            return
        if event.name != "conversation.turn.appended.v1" or "curate" not in event.payload.get(
            "jobs", []
        ):
            return
        turn_id = event.payload.get("turn_id")
        if not turn_id:
            raise ValueError("conversation curation event has no turn_id")
        result = await asyncio.to_thread(
            container.curator.curate_turn,
            CurateConversationTurnCommand(
                tenant_id=event.tenant_id,
                turn_id=UUID(str(turn_id)),
                auto_accept=True,
                idempotency_key=f"auto-curate-conversation-turn:{turn_id}",
            ),
        )
        log_event(
            "conversation_curation_completed",
            worker="maintenance",
            tenant_id=event.tenant_id,
            workspace_id=event.workspace_id,
            turn_id=turn_id,
            outcome="accepted" if result.retained is not None else "proposal",
        )

    consumer = IdempotentEventConsumer(
        container.store,  # type: ignore[arg-type]
        handler,
        consumer="maintenance-reflect-v1",
        worker_id=worker_id,
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
        dead_letter_max_bytes=int(os.getenv("UAM_NATS_DLQ_MAX_BYTES", "134217728")),
        dead_letter_max_age_seconds=int(
            os.getenv("UAM_NATS_DLQ_MAX_AGE_SECONDS", "1209600")
        ),
        auth_token=read_secret_env("UAM_NATS_AUTH_TOKEN"),
    )
    heartbeat = WorkerHeartbeatEmitter(
        container.store,  # type: ignore[arg-type]
        tenant_id=server_id,
        worker_kind="maintenance-worker",
        worker_id=worker_id,
        interval_seconds=float(os.getenv("UAM_WORKER_HEARTBEAT_SECONDS", "5")),
    )
    heartbeat_task: asyncio.Task[None] | None = None
    heartbeat_started = False
    try:
        await worker.connect()
        await heartbeat.start()
        heartbeat_started = True
        heartbeat_task = asyncio.create_task(heartbeat.run())
        log_event("worker_started", worker="maintenance")
        while True:
            acked = await worker.run_once(batch_size=10, timeout=0.5)
            if heartbeat_task is not None and heartbeat_task.done():
                heartbeat_task.result()
            if acked:
                log_event("worker_batch_completed", worker="maintenance", acknowledged=acked)
            if acked == 0:
                await asyncio.sleep(0.5)
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
                    worker="maintenance",
                    error_type=type(error).__name__,
                )
        log_event("worker_stopped", worker="maintenance")
        await worker.close()
        container.store.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
