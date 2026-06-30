"""Infrastructure ports. Services depend only on these protocols."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import MemoryItem, MemoryLayer, Observation


class MemoryLedger(Protocol):
    """Transactional append-only memory system of record."""

    def append(self, item: MemoryItem, idempotency_key: str | None = None) -> tuple[MemoryItem, bool]:
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


class ObservationRepository(Protocol):
    """Storage boundary for consolidated, evidence-grounded observations."""

    def save(self, observation: Observation) -> Observation:
        """Store an observation idempotently by its identity."""
        ...

    def list_for_workspace(self, tenant_id: UUID, workspace_id: UUID) -> tuple[Observation, ...]:
        """List tenant-safe observations for recall or audit."""
        ...
