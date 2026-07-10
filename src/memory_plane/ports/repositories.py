"""Infrastructure ports. Services depend only on these protocols."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import ClaimedEvent, ConsumerClaim, IntegrationEvent
from memory_plane.domain.api_key import ApiKeyRecord
from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.conflict import ConflictReviewDecision
from memory_plane.domain.graph import MemoryEdge, MemoryEdgeType
from memory_plane.domain.identity import AgentIdentity, ThreadIdentity
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

    def is_recallable_head(self, tenant_id: UUID, item_id: UUID) -> bool:
        """Return whether an item is the non-tombstoned head of its revision chain."""
        ...

    def filter_recallable_heads(
        self,
        tenant_id: UUID,
        item_ids: tuple[UUID, ...],
    ) -> frozenset[UUID]:
        """Return recallable IDs in one storage round trip."""
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

    def supersede_if_current(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        *,
        expected_revision: int,
        idempotency_key: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        """Append a replacement only if its parent is still the current head."""
        ...


class IdentityRegistry(Protocol):
    """Atomic stable identity registry for agents and their threads."""

    def provision_agent_thread(
        self,
        agent: AgentIdentity,
        *,
        thread_id: UUID | None = None,
        thread_status: str = "active",
    ) -> tuple[AgentIdentity, ThreadIdentity | None]:
        """Create/update an agent and optional thread without moving scope."""
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


class ConflictReviewRepository(Protocol):
    """Storage boundary for human decisions on conflict cases."""

    def save(self, decision: ConflictReviewDecision) -> ConflictReviewDecision:
        """Create or replace one review decision."""
        ...

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[ConflictReviewDecision, ...]:
        """List persisted decisions for a workspace."""
        ...


class GraphRepository(Protocol):
    """Storage boundary for typed memory graph edges."""

    def save_edge(self, edge: MemoryEdge) -> MemoryEdge:
        """Persist one graph edge idempotently by edge id."""
        ...

    def list_neighbors(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        item_id: UUID,
        *,
        edge_type: MemoryEdgeType | None = None,
    ) -> tuple[MemoryEdge, ...]:
        """List incoming and outgoing edges for one item."""
        ...


class AuditRepository(Protocol):
    """Append-only audit trail for operator and agent actions."""

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        """Persist one audit event."""
        ...

    def list_audit_events(
        self,
        tenant_id: UUID,
        *,
        workspace_id: UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        before_event_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        """List recent audit events under tenant/workspace filters."""
        ...

    def prune_audit_events(
        self,
        tenant_id: UUID,
        *,
        created_before: datetime,
        workspace_id: UUID | None = None,
        limit: int = 500,
    ) -> int:
        """Delete old audit events after external export has been verified."""
        ...


class ApiKeyRegistryRepository(Protocol):
    """Durable API-key metadata without bearer-secret storage."""

    def save_api_key_record(self, record: ApiKeyRecord) -> ApiKeyRecord:
        """Create or update one key metadata row."""
        ...

    def get_api_key_by_fingerprint(
        self, tenant_id: UUID, secret_fingerprint: str
    ) -> ApiKeyRecord | None:
        """Load one key by non-secret fingerprint."""
        ...

    def touch_api_key(
        self, tenant_id: UUID, secret_fingerprint: str, *, used_at: datetime
    ) -> ApiKeyRecord | None:
        """Update last-used metadata for one key."""
        ...

    def list_api_keys(self, tenant_id: UUID) -> tuple[ApiKeyRecord, ...]:
        """List key metadata under tenant boundary."""
        ...

    def revoke_api_key(
        self,
        tenant_id: UUID,
        key_id: UUID,
        *,
        revoked_at: datetime,
        reason: str = "",
    ) -> ApiKeyRecord:
        """Mark one key revoked."""
        ...
