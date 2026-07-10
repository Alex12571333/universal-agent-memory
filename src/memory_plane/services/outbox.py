"""Leased transactional-outbox relay with at-least-once delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from memory_plane.ports.repositories import EventSink, OutboxRepository


@dataclass(frozen=True, slots=True)
class RelayResult:
    """Inspectable outcome of one bounded polling cycle."""

    claimed: int
    published: int
    failed: int


class OutboxRelay:
    """Move committed PostgreSQL events to a durable transport."""

    def __init__(
        self,
        repository: OutboxRepository,
        sink: EventSink,
        *,
        tenant_id: UUID,
        worker_id: str,
        batch_size: int = 50,
        lease_seconds: int = 30,
        max_attempts: int = 8,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        if not 1 <= batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_base_seconds < 1:
            raise ValueError("retry_base_seconds must be positive")
        if retry_max_seconds < retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        self._repository = repository
        self._sink = sink
        self._tenant_id = tenant_id
        self._worker_id = worker_id
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    async def run_once(self) -> RelayResult:
        """Claim a batch, publish each event and ack only confirmed sends."""
        claimed = await asyncio.to_thread(
            self._repository.claim_outbox,
            self._tenant_id,
            self._worker_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        published = 0
        failed = 0
        for row in claimed:
            try:
                await self._sink.send(row.event)
            except Exception as error:
                failed += 1
                await asyncio.to_thread(
                    self._repository.release_outbox,
                    self._tenant_id,
                    row.event.id,
                    self._worker_id,
                    error=f"{type(error).__name__}: {error}"[:2000],
                    max_attempts=self._max_attempts,
                    retry_delay_seconds=self._retry_delay(row.attempts),
                )
                continue
            acknowledged = await asyncio.to_thread(
                self._repository.mark_outbox_published,
                self._tenant_id,
                row.event.id,
                self._worker_id,
            )
            published += int(acknowledged)
        return RelayResult(claimed=len(claimed), published=published, failed=failed)

    def _retry_delay(self, attempts: int) -> int:
        """Return capped exponential delay after the claimed delivery attempt."""
        return min(self._retry_max_seconds, self._retry_base_seconds * (2 ** (attempts - 1)))
