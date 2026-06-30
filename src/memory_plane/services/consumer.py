"""Durable idempotency wrapper for at-least-once event consumers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from memory_plane.contracts.events import ConsumerClaim, IntegrationEvent
from memory_plane.ports.repositories import ProcessedEventRepository

AsyncEventHandler = Callable[[IntegrationEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ConsumerResult:
    """Outcome used by transports to ack, nak or ignore a delivery."""

    processed: bool
    duplicate: bool = False
    busy: bool = False


class IdempotentEventConsumer:
    """Prevent concurrent duplicates and remember completed event IDs."""

    def __init__(
        self,
        repository: ProcessedEventRepository,
        handler: AsyncEventHandler,
        *,
        consumer: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        if not consumer.strip() or not worker_id.strip():
            raise ValueError("consumer and worker_id must not be empty")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._repository = repository
        self._handler = handler
        self._consumer = consumer
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    async def handle(self, event: IntegrationEvent) -> ConsumerResult:
        """Run one handler after acquiring its durable deduplication lease."""
        claim = await asyncio.to_thread(
            self._repository.claim_event_processing,
            event.tenant_id,
            event.id,
            self._consumer,
            self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if claim == ConsumerClaim.COMPLETED:
            return ConsumerResult(processed=False, duplicate=True)
        if claim == ConsumerClaim.BUSY:
            return ConsumerResult(processed=False, busy=True)
        try:
            await self._handler(event)
        except Exception as error:
            await asyncio.to_thread(
                self._repository.release_event_processing,
                event.tenant_id,
                event.id,
                self._consumer,
                self._worker_id,
                error=f"{type(error).__name__}: {error}"[:2000],
            )
            raise
        completed = await asyncio.to_thread(
            self._repository.complete_event_processing,
            event.tenant_id,
            event.id,
            self._consumer,
            self._worker_id,
        )
        return ConsumerResult(processed=completed)
