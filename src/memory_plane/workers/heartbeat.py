"""Shared asynchronous writer for durable worker liveness assertions."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from uuid import UUID

from memory_plane.domain.worker import WorkerHeartbeat
from memory_plane.ports.repositories import WorkerHeartbeatRepository


class WorkerHeartbeatEmitter:
    """Write one immediate heartbeat and refresh it at a bounded interval."""

    def __init__(
        self,
        repository: WorkerHeartbeatRepository,
        *,
        tenant_id: UUID,
        worker_kind: str,
        worker_id: str,
        interval_seconds: float = 5.0,
    ) -> None:
        if not 0.5 <= interval_seconds <= 300:
            raise ValueError("worker heartbeat interval must be between 0.5 and 300 seconds")
        self._repository = repository
        self._tenant_id = tenant_id
        self._worker_kind = worker_kind
        self._worker_id = worker_id
        self._interval_seconds = interval_seconds
        self._started_at = datetime.now(UTC)
        self._next_due = 0.0

    async def start(self) -> None:
        """Publish the initial running state before entering the work loop."""
        await self._write("running")

    async def tick(self) -> bool:
        """Refresh when due and report whether a database write occurred."""
        if time.monotonic() < self._next_due:
            return False
        await self._write("running")
        return True

    async def run(self) -> None:
        """Refresh independently so long-running jobs do not look dead."""
        while True:
            await asyncio.sleep(self._interval_seconds)
            await self._write("running")

    async def stop(self) -> None:
        """Mark a graceful shutdown immediately instead of waiting for TTL expiry."""
        await self._write("stopping")

    async def _write(self, status: str) -> None:
        heartbeat = WorkerHeartbeat(
            tenant_id=self._tenant_id,
            worker_kind=self._worker_kind,
            worker_id=self._worker_id,
            started_at=self._started_at,
            last_seen_at=datetime.now(UTC),
            status=status,
        )
        await asyncio.to_thread(self._repository.record_worker_heartbeat, heartbeat)
        self._next_due = time.monotonic() + self._interval_seconds
