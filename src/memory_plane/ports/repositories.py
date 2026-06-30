"""Infrastructure ports. Services depend only on these protocols."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import ClaimedEvent, ConsumerClaim, IntegrationEvent
from memory_plane.domain.models import MemoryItem, MemoryLayer, Observation


class MemoryLedger(Protocol):
    """Transactional append-only memory system of record."""

    def append(
        self, item: MemoryItem, idempotency_key: str | None = None
    ) -> tuple[MemoryItem, bool]:
        """Append an item or return the existing item for the same idempotency key."""
        ...

    def get(self, tenant_id: UUID, item_id: UUID) -> MemoryItem | None:
        """Load one item while enforcing the tenant boundary."""
        ...

    def list_for_workspace(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        layers: tuple[MemoryLayer, ...] = (),
    ) -> tuple[MemoryItem, ...]:
        """List canonical items for deterministic fallback and maintenance jobs."""
        ...


class RetentionStore(MemoryLedger, Protocol):
    """Atomic boundary for canonical memory and its outbox event."""

    def retain(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        idempotency_key: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        """Append memory and event in one transaction, or return an earlier result."""
        ...


class CandidateSource(Protocol):
    """One independently deployable retrieval strategy."""

    @property
    def name(self) -> str:
        """Stable diagnostic name included in retrieval traces."""
        ...

    def search(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """Produce candidates with source-specific normalized signals."""
        ...


class EventPublisher(Protocol):
    """Transactional outbox or broker-facing event sink."""

    def publish(self, event: IntegrationEvent) -> None:
        """Persist an event for at-least-once delivery."""
        ...


class OutboxRepository(Protocol):
    """Leased PostgreSQL outbox boundary for at-least-once delivery."""

    def claim_outbox(
        self,
        tenant_id: UUID,
        worker_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> tuple[ClaimedEvent, ...]:
        """Lease due events using skip-locked concurrency."""
        ...

    def mark_outbox_published(
        self, tenant_id: UUID, event_id: UUID, worker_id: str
    ) -> bool:
        """Acknowledge a leased event after the sink confirms publication."""
        ...

    def release_outbox(
        self,
        tenant_id: UUID,
        event_id: UUID,
        worker_id: str,
        *,
        error: str,
        max_attempts: int,
    ) -> bool:
        """Release for retry or dead-letter after the configured attempt limit."""
        ...


class EventSink(Protocol):
    """Asynchronous transport receiving integration events from the outbox."""

    async def send(self, event: IntegrationEvent) -> None:
        """Confirm only after the transport durably accepts the event."""
        ...


class ProcessedEventRepository(Protocol):
    """Durable per-consumer idempotency and active-processing leases."""

    def claim_event_processing(
        self,
        tenant_id: UUID,
        event_id: UUID,
        consumer: str,
        worker_id: str,
        *,
        lease_seconds: int,
    ) -> ConsumerClaim:
        """Acquire, reject as busy, or report an already completed event."""
        ...

    def complete_event_processing(
        self,
        tenant_id: UUID,
        event_id: UUID,
        consumer: str,
        worker_id: str,
    ) -> bool:
        """Mark a successfully handled event permanently complete."""
        ...

    def release_event_processing(
        self,
        tenant_id: UUID,
        event_id: UUID,
        consumer: str,
        worker_id: str,
        *,
        error: str,
    ) -> bool:
        """Release a failed handler attempt for later redelivery."""
        ...


class ObservationRepository(Protocol):
    """Storage boundary for consolidated, evidence-grounded observations."""

    def save(self, observation: Observation) -> Observation:
        """Store an observation idempotently by its identity."""
        ...

    def list_for_workspace(self, tenant_id: UUID, workspace_id: UUID) -> tuple[Observation, ...]:
        """List tenant-safe observations for recall or audit."""
        ...
