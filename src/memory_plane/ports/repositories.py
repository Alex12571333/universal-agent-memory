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
from memory_plane.domain.identity import AgentIdentity, ThreadIdentity, WorkspaceIdentity
from memory_plane.domain.models import MemoryItem, MemoryLayer, Observation
from memory_plane.domain.worker import WorkerHeartbeat, WorkerReadiness


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
        audit_event: AuditEvent | None = None,
    ) -> tuple[MemoryItem, bool]:
        """Append a replacement only if its parent is still the current head."""
        ...


class IdentityRegistry(Protocol):
    """Atomic stable identity registry for agents and their threads."""

    def provision_workspace(self, workspace: WorkspaceIdentity) -> WorkspaceIdentity:
        """Create or return a workspace without allowing a cross-tenant move."""
        ...

    def provision_agent_thread(
        self,
        agent: AgentIdentity,
        *,
        thread_id: UUID | None = None,
        thread_status: str = "active",
    ) -> tuple[AgentIdentity, ThreadIdentity | None]:
        """Create/update an agent and optional thread without moving scope."""
        ...

    def thread_belongs_to_agent(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        thread_id: UUID,
    ) -> bool:
        """Return whether a thread is owned by the bound agent in scope."""
        ...


class RetentionStore(MemoryLedger, Protocol):
    """Atomic boundary for canonical memory and its outbox event."""

    def retain(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        idempotency_key: str | None = None,
        audit_event: AuditEvent | None = None,
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
        retry_delay_seconds: int,
    ) -> bool:
        """Schedule an exponential retry or dead-letter after the attempt limit."""
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

    def save(
        self, observation: Observation, audit_event: AuditEvent | None = None
    ) -> Observation:
        """Store an observation idempotently by its identity."""
        ...

    def list_for_workspace(self, tenant_id: UUID, workspace_id: UUID) -> tuple[Observation, ...]:
        """List tenant-safe observations for recall or audit."""
        ...


class ConflictReviewRepository(Protocol):
    """Storage boundary for human decisions on conflict cases."""

    def save(
        self, decision: ConflictReviewDecision, audit_event: AuditEvent | None = None
    ) -> ConflictReviewDecision:
        """Create or replace one review decision."""
        ...

    def apply_resolution(
        self,
        decision: ConflictReviewDecision,
        writes: tuple[tuple[MemoryItem, IntegrationEvent, int], ...],
        audit_event: AuditEvent | None = None,
    ) -> ConflictReviewDecision:
        """Atomically append resolution revisions/events and persist the review."""
        ...

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[ConflictReviewDecision, ...]:
        """List persisted decisions for a workspace."""
        ...


class GraphRepository(Protocol):
    """Storage boundary for typed memory graph edges."""

    def save_edge(self, edge: MemoryEdge, audit_event: AuditEvent | None = None) -> MemoryEdge:
        """Persist one graph edge idempotently by edge id."""
        ...

    def list_neighbors(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        item_id: UUID,
        *,
        edge_type: MemoryEdgeType | None = None,
        after_created_at: datetime | None = None,
        after_edge_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[MemoryEdge, ...]:
        """List incoming and outgoing edges for one item."""
        ...

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[MemoryEdge, ...]:
        """List all typed graph edges in one tenant-scoped workspace."""
        ...


class AuditRepository(Protocol):
    """Append-only audit trail for operator and agent actions."""

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        """Persist one audit event."""
        ...

    def get_audit_event(self, tenant_id: UUID, event_id: UUID) -> AuditEvent | None:
        """Load one tenant-scoped audit event for an operator replay."""
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


class WorkerHeartbeatRepository(Protocol):
    """Durable tenant-scoped liveness ledger for asynchronous workers."""

    def record_worker_heartbeat(self, heartbeat: WorkerHeartbeat) -> WorkerHeartbeat:
        """Upsert the latest state for one process identity."""
        ...

    def worker_readiness(
        self,
        tenant_id: UUID,
        required_kinds: tuple[str, ...],
        *,
        stale_after_seconds: int,
    ) -> WorkerReadiness:
        """Aggregate required kinds without exposing worker identities."""
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
