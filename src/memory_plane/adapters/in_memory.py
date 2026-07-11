"""Deterministic development adapter implementing all core ports."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.api_key import ApiKeyRecord
from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.domain.conflict import ConflictReviewDecision
from memory_plane.domain.conversation import (
    PURGED_CONVERSATION_CONTENT,
    ConversationMessage,
    ConversationTurn,
)
from memory_plane.domain.graph import MemoryEdge, MemoryEdgeType
from memory_plane.domain.identity import AgentIdentity, ThreadIdentity, WorkspaceIdentity
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    MemoryStatus,
    Observation,
)
from memory_plane.domain.proposal import MemoryProposal, MemoryProposalStatus

_WORD = re.compile(r"\w+", re.UNICODE)


def _scope_idempotency_key(workspace_id: UUID, key: str | None) -> str | None:
    """Prevent one tenant's independent workspaces from colliding on retries."""
    return None if key is None else f"{workspace_id}:{key}"


class InMemoryMemoryStore:
    """Thread-safe fake ledger, outbox, observation store and lexical source."""

    def __init__(self) -> None:
        """Initialize isolated mutable state for one test or local process."""
        self._items: dict[UUID, MemoryItem] = {}
        self._idempotency: dict[tuple[UUID, str], UUID] = {}
        self._observations: dict[UUID, Observation] = {}
        self._conflict_reviews: dict[tuple[UUID, UUID], ConflictReviewDecision] = {}
        self._edges: dict[UUID, MemoryEdge] = {}
        self._turns: dict[UUID, ConversationTurn] = {}
        self._turn_idempotency: dict[tuple[UUID, str], UUID] = {}
        self._proposals: dict[UUID, MemoryProposal] = {}
        self._proposal_idempotency: dict[tuple[UUID, str], UUID] = {}
        self._audit_events: dict[UUID, AuditEvent] = {}
        self._api_keys: dict[UUID, ApiKeyRecord] = {}
        self._workspaces: dict[UUID, WorkspaceIdentity] = {}
        self._agents: dict[UUID, AgentIdentity] = {}
        self._threads: dict[UUID, ThreadIdentity] = {}
        self.events: list[IntegrationEvent] = []
        self._lock = RLock()

    @property
    def name(self) -> str:
        """Return the stable retrieval diagnostic name."""
        return "sql_lexical"

    def ping(self) -> bool:
        """In-process canonical storage is ready while the process is alive."""
        return True

    def provision_agent_thread(
        self,
        agent: AgentIdentity,
        *,
        thread_id: UUID | None = None,
        thread_status: str = "active",
    ) -> tuple[AgentIdentity, ThreadIdentity | None]:
        """Create/update an identity while forbidding cross-scope ID reuse."""
        with self._lock:
            existing_agent = self._agents.get(agent.id)
            if existing_agent is not None and (
                existing_agent.tenant_id != agent.tenant_id
                or existing_agent.workspace_id != agent.workspace_id
            ):
                raise ValueError("agent_id already belongs to another scope")
            self._agents[agent.id] = agent
            thread: ThreadIdentity | None = None
            if thread_id is not None:
                existing_thread = self._threads.get(thread_id)
                if existing_thread is not None and (
                    existing_thread.tenant_id != agent.tenant_id
                    or existing_thread.workspace_id != agent.workspace_id
                ):
                    raise ValueError("thread_id already belongs to another scope")
                thread = ThreadIdentity(
                    id=thread_id,
                    tenant_id=agent.tenant_id,
                    workspace_id=agent.workspace_id,
                    owner_agent_id=agent.id,
                    status=thread_status,
                )
                self._threads[thread_id] = thread
            return agent, thread

    def provision_workspace(self, workspace: WorkspaceIdentity) -> WorkspaceIdentity:
        """Create/update an in-memory workspace without changing tenant ownership."""
        with self._lock:
            existing = self._workspaces.get(workspace.id)
            if existing is not None and existing.tenant_id != workspace.tenant_id:
                raise ValueError("workspace_id already belongs to another tenant")
            self._workspaces[workspace.id] = workspace
            return workspace

    def thread_belongs_to_agent(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
        thread_id: UUID,
    ) -> bool:
        """Validate an owned thread under its full identity boundary."""
        thread = self._threads.get(thread_id)
        return bool(
            thread
            and thread.tenant_id == tenant_id
            and thread.workspace_id == workspace_id
            and thread.owner_agent_id == agent_id
        )

    def append(
        self, item: MemoryItem, idempotency_key: str | None = None
    ) -> tuple[MemoryItem, bool]:
        """Atomically append or return an idempotent prior result."""
        idempotency_key = _scope_idempotency_key(item.workspace_id, idempotency_key)
        with self._lock:
            if idempotency_key:
                key = (item.tenant_id, idempotency_key)
                existing_id = self._idempotency.get(key)
                if existing_id is not None:
                    return self._items[existing_id], False
            self._items[item.id] = item
            if idempotency_key:
                self._idempotency[(item.tenant_id, idempotency_key)] = item.id
            return item, True

    def retain(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        idempotency_key: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        """Atomically append canonical memory and its outbox event."""
        with self._lock:
            stored, created = self.append(item, idempotency_key)
            if created:
                self.publish(event)
            return stored, created

    def supersede_if_current(
        self,
        item: MemoryItem,
        event: IntegrationEvent,
        *,
        expected_revision: int,
        idempotency_key: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        """CAS append a replacement and enqueue its derived-work event."""
        idempotency_key = _scope_idempotency_key(item.workspace_id, idempotency_key)
        if item.supersedes_id is None:
            raise ValueError("replacement item must declare supersedes_id")
        with self._lock:
            if idempotency_key:
                key = (item.tenant_id, idempotency_key)
                existing_id = self._idempotency.get(key)
                if existing_id is not None:
                    return self._items[existing_id], False

            parent = self.get(item.tenant_id, item.supersedes_id)
            if parent is None:
                raise KeyError("memory item not found")
            child = self._latest_descendant(parent)
            actual = child.revision
            if child.id != parent.id or parent.revision != expected_revision:
                raise MemoryRevisionConflictError(
                    item.supersedes_id, expected_revision, actual
                )

            self._items[item.id] = item
            if idempotency_key:
                self._idempotency[(item.tenant_id, idempotency_key)] = item.id
            self.publish(event)
            return item, True

    def get(self, tenant_id: UUID, item_id: UUID) -> MemoryItem | None:
        """Return an item only when its tenant matches exactly."""
        item = self._items.get(item_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    def is_recallable_head(self, tenant_id: UUID, item_id: UUID) -> bool:
        """Reject tombstones and every item with a newer revision child."""
        item = self.get(tenant_id, item_id)
        if item is None or item.status in (MemoryStatus.REJECTED, MemoryStatus.ARCHIVED):
            return False
        return not any(
            candidate.tenant_id == tenant_id and candidate.supersedes_id == item_id
            for candidate in self._items.values()
        )

    def filter_recallable_heads(
        self,
        tenant_id: UUID,
        item_ids: tuple[UUID, ...],
    ) -> frozenset[UUID]:
        """Return active heads from a bounded ID batch."""
        return frozenset(
            item_id for item_id in item_ids if self.is_recallable_head(tenant_id, item_id)
        )

    def _latest_descendant(self, item: MemoryItem) -> MemoryItem:
        """Follow the append-only supersedes chain to its latest known head."""
        head = item
        changed = True
        while changed:
            changed = False
            for candidate in self._items.values():
                if (
                    candidate.tenant_id == item.tenant_id
                    and candidate.supersedes_id == head.id
                    and candidate.revision > head.revision
                ):
                    head = candidate
                    changed = True
                    break
        return head

    def list_for_workspace(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        layers: tuple[MemoryLayer, ...] = (),
        status: MemoryStatus | None = None,
        label: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[MemoryItem, ...]:
        """List workspace items in creation order with optional layer filtering."""
        rows = [
            item
            for item in self._items.values()
            if item.tenant_id == tenant_id
            and item.workspace_id == workspace_id
            and (not layers or item.layer in layers)
            and (status is None or item.status == status)
            and (not label or label in item.labels)
        ]
        ordered = sorted(rows, key=lambda item: (item.created_at, item.id))
        return tuple(ordered[max(0, offset) : None if limit is None else max(0, offset) + limit])

    def search(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """Provide portable lexical candidates and strict metadata filtering."""
        query_terms = self._terms(query.text)
        rows: list[Candidate] = []
        all_items = self.list_for_workspace(
            query.tenant_id, query.workspace_id, layers=query.layers
        )
        for item in all_items:
            if not self.is_recallable_head(query.tenant_id, item.id):
                continue
            if item.scope == MemoryScope.THREAD and item.thread_id != query.thread_id:
                continue
            if item.scope == MemoryScope.PRIVATE and item.agent_id != query.agent_id:
                continue
            if query.labels and not set(query.labels).issubset(item.labels):
                continue
            item_terms = self._terms(item.text)
            overlap = len(query_terms & item_terms)
            lexical = overlap / max(1, len(query_terms))
            if lexical > 0 or item.layer in (MemoryLayer.CORE, MemoryLayer.WORKING):
                rows.append(
                    Candidate(
                        item=item,
                        source=self.name,
                        lexical=lexical,
                        entity=lexical,
                        trust=item.confidence,
                    )
                )
        return tuple(rows)

    def publish(self, event: IntegrationEvent) -> None:
        """Append an outbox event once by event ID."""
        with self._lock:
            if all(existing.id != event.id for existing in self.events):
                self.events.append(event)

    def append_turn(
        self, turn: ConversationTurn, idempotency_key: str | None = None
    ) -> tuple[ConversationTurn, bool]:
        """Atomically append a raw conversation turn."""
        idempotency_key = _scope_idempotency_key(turn.workspace_id, idempotency_key)
        with self._lock:
            if idempotency_key:
                key = (turn.tenant_id, idempotency_key)
                existing_id = self._turn_idempotency.get(key)
                if existing_id is not None:
                    return self._turns[existing_id], False
            self._turns[turn.id] = turn
            if idempotency_key:
                self._turn_idempotency[(turn.tenant_id, idempotency_key)] = turn.id
            return turn, True

    def get_turn(self, tenant_id: UUID, turn_id: UUID) -> ConversationTurn | None:
        """Return a raw conversation turn only for its owning tenant."""
        turn = self._turns.get(turn_id)
        return turn if turn is not None and turn.tenant_id == tenant_id else None

    def list_turns(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        thread_id: UUID | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> tuple[ConversationTurn, ...]:
        """List recent raw conversation turns."""
        rows = [
            turn
            for turn in self._turns.values()
            if turn.tenant_id == tenant_id
            and turn.workspace_id == workspace_id
            and (thread_id is None or turn.thread_id == thread_id)
            and (namespace is None or turn.namespace == namespace)
        ]
        rows.sort(key=lambda turn: (turn.created_at, turn.id), reverse=True)
        return tuple(rows[:limit])

    def purge_turn_content(self, tenant_id: UUID, turn_id: UUID) -> bool:
        """Replace transcript text after curated-only curation, preserving audit IDs."""
        return self._purge_turn_content(tenant_id, turn_id, "purged_after_curation")

    def _purge_turn_content(self, tenant_id: UUID, turn_id: UUID, reason: str) -> bool:
        """Replace transcript content and preserve the reason in immutable audit metadata."""
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None or turn.tenant_id != tenant_id:
                return False
            retention = {"raw_content": reason}
            self._turns[turn_id] = ConversationTurn(
                id=turn.id,
                tenant_id=turn.tenant_id,
                workspace_id=turn.workspace_id,
                thread_id=turn.thread_id,
                agent_id=turn.agent_id,
                namespace=turn.namespace,
                source_kind=turn.source_kind,
                retention_policy=turn.retention_policy,
                created_at=turn.created_at,
                expires_at=turn.expires_at,
                metadata={**turn.metadata, "retention": retention},
                messages=tuple(
                    ConversationMessage(
                        role=message.role,
                        name=message.name,
                        content=PURGED_CONVERSATION_CONTENT,
                        metadata={**message.metadata, "retention": retention},
                    )
                    for message in turn.messages
                ),
            )
            return True

    def purge_expired_turns(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[UUID, ...]:
        """Purge due staged transcripts without deleting their audit identity."""
        with self._lock:
            turn_ids = tuple(
                turn.id
                for turn in sorted(
                    self._turns.values(),
                    key=lambda item: (item.expires_at or now, item.id),
                )
                if turn.tenant_id == tenant_id
                and turn.workspace_id == workspace_id
                and turn.expires_at is not None
                and turn.expires_at <= now
                and any(message.content != PURGED_CONVERSATION_CONTENT for message in turn.messages)
            )[:limit]
        for turn_id in turn_ids:
            self._purge_turn_content(tenant_id, turn_id, "purged_after_expiry")
        return turn_ids

    def append_proposal(
        self, proposal: MemoryProposal, idempotency_key: str | None = None
    ) -> tuple[MemoryProposal, bool]:
        """Atomically append a memory proposal."""
        idempotency_key = _scope_idempotency_key(proposal.workspace_id, idempotency_key)
        with self._lock:
            if idempotency_key:
                key = (proposal.tenant_id, idempotency_key)
                existing_id = self._proposal_idempotency.get(key)
                if existing_id is not None:
                    return self._proposals[existing_id], False
            self._proposals[proposal.id] = proposal
            if idempotency_key:
                self._proposal_idempotency[(proposal.tenant_id, idempotency_key)] = (
                    proposal.id
                )
            return proposal, True

    def get_proposal(self, tenant_id: UUID, proposal_id: UUID) -> MemoryProposal | None:
        """Return a memory proposal only for its owning tenant."""
        proposal = self._proposals.get(proposal_id)
        return (
            proposal
            if proposal is not None and proposal.tenant_id == tenant_id
            else None
        )

    def save_proposal_review(self, proposal: MemoryProposal) -> MemoryProposal:
        """Persist a reviewed proposal copy."""
        with self._lock:
            current = self.get_proposal(proposal.tenant_id, proposal.id)
            if current is None:
                raise KeyError("memory proposal not found")
            self._proposals[proposal.id] = proposal
            return proposal

    def accept_proposal_with_memory(
        self,
        proposal: MemoryProposal,
        item: MemoryItem,
        event: IntegrationEvent,
        *,
        reviewer: str,
        reason: str,
        idempotency_key: str,
    ) -> tuple[MemoryProposal, MemoryItem, bool]:
        """Atomically transition an open proposal and append its memory/event."""
        idempotency_key = _scope_idempotency_key(item.workspace_id, idempotency_key)
        with self._lock:
            current = self.get_proposal(proposal.tenant_id, proposal.id)
            if current is None:
                raise KeyError("memory proposal not found")
            if current.status == MemoryProposalStatus.REJECTED:
                raise ValueError("rejected proposal cannot be accepted")
            existing_id = self._idempotency.get((item.tenant_id, idempotency_key))
            if existing_id is not None:
                return current, self._items[existing_id], False
            if current.status == MemoryProposalStatus.ACCEPTED:
                accepted_id = current.metadata.get("accepted_memory_id")
                if isinstance(accepted_id, str):
                    for memory_id, memory in self._items.items():
                        if str(memory_id) == accepted_id:
                            return current, memory, False
                raise RuntimeError("accepted proposal is missing its durable memory")
            self._items[item.id] = item
            self._idempotency[(item.tenant_id, idempotency_key)] = item.id
            self.publish(event)
            reviewed = replace(
                current,
                status=MemoryProposalStatus.ACCEPTED,
                reviewed_at=datetime.now(UTC),
                reviewer=reviewer.strip()[:120] or "operator",
                review_reason=reason.strip()[:1000],
                metadata={**current.metadata, "accepted_memory_id": str(item.id)},
            )
            self._proposals[current.id] = reviewed
            return reviewed, item, True

    def list_proposals(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        namespace: str | None = None,
        status: MemoryProposalStatus | None = None,
        limit: int = 50,
    ) -> tuple[MemoryProposal, ...]:
        """List recent memory proposals."""
        rows = [
            proposal
            for proposal in self._proposals.values()
            if proposal.tenant_id == tenant_id
            and proposal.workspace_id == workspace_id
            and (namespace is None or proposal.namespace == namespace)
            and (status is None or proposal.status == status)
        ]
        rows.sort(key=lambda proposal: (proposal.created_at, proposal.id), reverse=True)
        return tuple(rows[:limit])

    def collect_metrics(self, tenant_id: UUID | None = None) -> dict[str, float | int]:
        """Return lightweight local counters for the standalone/dev adapter."""
        with self._lock:
            return {
                "memory_items_total": len(
                    [
                        item
                        for item in self._items.values()
                        if tenant_id is None or item.tenant_id == tenant_id
                    ]
                ),
                "observations_total": len(
                    [
                        item
                        for item in self._observations.values()
                        if tenant_id is None or item.tenant_id == tenant_id
                    ]
                ),
                "outbox_pending_total": len(
                    [
                        event
                        for event in self.events
                        if tenant_id is None or event.tenant_id == tenant_id
                    ]
                ),
                "outbox_dead_letter_total": 0,
                "outbox_lag_seconds": 0.0,
                "processed_events_inflight_total": 0,
                "checkpoints_total": 0,
                "audit_events_total": len(
                    [
                        event
                        for event in self._audit_events.values()
                        if tenant_id is None or event.tenant_id == tenant_id
                    ]
                ),
                "api_keys_total": len(
                    [
                        record
                        for record in self._api_keys.values()
                        if tenant_id is None or record.tenant_id == tenant_id
                    ]
                ),
                "api_keys_revoked_total": len(
                    [
                        record
                        for record in self._api_keys.values()
                        if (tenant_id is None or record.tenant_id == tenant_id)
                        and record.revoked
                    ]
                ),
            }

    def append_audit_event(self, event: AuditEvent) -> AuditEvent:
        """Append one immutable audit event."""
        with self._lock:
            self._audit_events.setdefault(event.id, event)
            return self._audit_events[event.id]

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
        """List recent audit events for operator review."""
        rows = [
            event
            for event in self._audit_events.values()
            if event.tenant_id == tenant_id
            and (workspace_id is None or event.workspace_id == workspace_id)
            and (action is None or event.action == action)
            and (resource_type is None or event.resource_type == resource_type)
            and (created_after is None or event.created_at >= created_after)
            and (
                created_before is None
                or event.created_at < created_before
                or (
                    before_event_id is not None
                    and event.created_at == created_before
                    and str(event.id) < str(before_event_id)
                )
            )
        ]
        rows.sort(key=lambda event: (event.created_at, event.id), reverse=True)
        return tuple(rows[:limit])

    def prune_audit_events(
        self,
        tenant_id: UUID,
        *,
        created_before: datetime,
        workspace_id: UUID | None = None,
        limit: int = 500,
    ) -> int:
        """Delete old audit events after a verified export."""
        safe_limit = max(1, min(int(limit), 500))
        with self._lock:
            doomed = [
                event
                for event in self._audit_events.values()
                if event.tenant_id == tenant_id
                and event.created_at < created_before
                and (workspace_id is None or event.workspace_id == workspace_id)
            ]
            doomed.sort(key=lambda event: (event.created_at, event.id))
            deleted = 0
            for event in doomed[:safe_limit]:
                if self._audit_events.pop(event.id, None) is not None:
                    deleted += 1
            return deleted

    def save_api_key_record(self, record: ApiKeyRecord) -> ApiKeyRecord:
        """Create/update one API key metadata row."""
        with self._lock:
            self._api_keys[record.id] = record
            return record

    def get_api_key_by_fingerprint(
        self, tenant_id: UUID, secret_fingerprint: str
    ) -> ApiKeyRecord | None:
        """Find one API key by non-secret fingerprint."""
        with self._lock:
            for record in self._api_keys.values():
                if (
                    record.tenant_id == tenant_id
                    and record.secret_fingerprint == secret_fingerprint
                ):
                    return record
        return None

    def touch_api_key(
        self,
        tenant_id: UUID,
        secret_fingerprint: str,
        *,
        used_at: datetime,
    ) -> ApiKeyRecord | None:
        """Update last-used metadata for one API key."""
        with self._lock:
            record = self.get_api_key_by_fingerprint(tenant_id, secret_fingerprint)
            if record is None:
                return None
            updated = ApiKeyRecord(
                id=record.id,
                tenant_id=record.tenant_id,
                name=record.name,
                secret_fingerprint=record.secret_fingerprint,
                scopes=record.scopes,
                created_at=record.created_at,
                last_used_at=used_at,
                revoked_at=record.revoked_at,
                revoked_reason=record.revoked_reason,
            )
            self._api_keys[record.id] = updated
            return updated

    def list_api_keys(self, tenant_id: UUID) -> tuple[ApiKeyRecord, ...]:
        """List key metadata for one tenant."""
        with self._lock:
            rows = [
                record
                for record in self._api_keys.values()
                if record.tenant_id == tenant_id
            ]
        rows.sort(key=lambda record: (record.name, record.created_at, record.id))
        return tuple(rows)

    def revoke_api_key(
        self,
        tenant_id: UUID,
        key_id: UUID,
        *,
        revoked_at: datetime,
        reason: str = "",
    ) -> ApiKeyRecord:
        """Mark one API key revoked."""
        with self._lock:
            record = self._api_keys.get(key_id)
            if record is None or record.tenant_id != tenant_id:
                raise KeyError("api key not found")
            updated = ApiKeyRecord(
                id=record.id,
                tenant_id=record.tenant_id,
                name=record.name,
                secret_fingerprint=record.secret_fingerprint,
                scopes=record.scopes,
                created_at=record.created_at,
                last_used_at=record.last_used_at,
                revoked_at=revoked_at,
                revoked_reason=reason,
            )
            self._api_keys[key_id] = updated
            return updated

    def save(self, observation: Observation) -> Observation:
        """Store a derived observation without mutating evidence."""
        with self._lock:
            self._observations.setdefault(observation.id, observation)
            return self._observations[observation.id]

    def list_observations(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[Observation, ...]:
        """List observations through an unambiguous convenience name."""
        return tuple(
            row
            for row in self._observations.values()
            if row.tenant_id == tenant_id and row.workspace_id == workspace_id
        )

    def save_conflict_review(
        self, decision: ConflictReviewDecision
    ) -> ConflictReviewDecision:
        """Persist or replace a human conflict-review decision."""
        with self._lock:
            self._conflict_reviews[(decision.tenant_id, decision.case_id)] = decision
            return decision

    def apply_conflict_resolution(
        self,
        decision: ConflictReviewDecision,
        writes: tuple[tuple[MemoryItem, IntegrationEvent, int], ...],
    ) -> ConflictReviewDecision:
        """Atomically validate then apply resolution revisions/events and review."""
        with self._lock:
            key = (decision.tenant_id, decision.case_id)
            existing = self._conflict_reviews.get(key)
            if (
                existing is not None
                and existing.status == decision.status
                and existing.winner_value == decision.winner_value
                and existing.applied_memory_id is not None
            ):
                return existing
            if existing is not None and existing.applied_memory_id is not None:
                raise ValueError("conflict resolution is already applied and immutable")
            for item, _event, expected_revision in writes:
                if (
                    item.tenant_id != decision.tenant_id
                    or item.workspace_id != decision.workspace_id
                ):
                    raise ValueError("conflict resolution write crosses decision scope")
                if item.supersedes_id is None:
                    raise ValueError(
                        "conflict resolution replacement must declare supersedes_id"
                    )
                parent = self.get(item.tenant_id, item.supersedes_id)
                if parent is None:
                    raise KeyError("conflict evidence memory not found")
                head = self._latest_descendant(parent)
                if head.id != parent.id or parent.revision != expected_revision:
                    raise MemoryRevisionConflictError(
                        parent.id,
                        expected_revision,
                        head.revision,
                    )
            for item, event, _expected_revision in writes:
                self._items[item.id] = item
                self.events.append(event)
            self._conflict_reviews[key] = decision
            return decision

    def list_conflict_reviews(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[ConflictReviewDecision, ...]:
        """List persisted conflict-review decisions."""
        return tuple(
            row
            for row in self._conflict_reviews.values()
            if row.tenant_id == tenant_id and row.workspace_id == workspace_id
        )

    def save_edge(self, edge: MemoryEdge) -> MemoryEdge:
        """Persist one memory graph edge."""
        with self._lock:
            self._edges.setdefault(edge.id, edge)
            return self._edges[edge.id]

    def list_neighbors(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        item_id: UUID,
        *,
        edge_type: MemoryEdgeType | None = None,
    ) -> tuple[MemoryEdge, ...]:
        """List incoming and outgoing graph edges."""
        rows = [
            edge
            for edge in self._edges.values()
            if edge.tenant_id == tenant_id
            and edge.workspace_id == workspace_id
            and (edge.src_id == item_id or edge.dst_id == item_id)
            and (edge_type is None or edge.edge_type == edge_type)
        ]
        return tuple(sorted(rows, key=lambda row: (row.created_at, row.id)))

    @staticmethod
    def _terms(text: str) -> set[str]:
        """Tokenize text for a dependency-free lexical fallback."""
        return {match.group(0).casefold() for match in _WORD.finditer(text)}


class InMemoryObservationRepository:
    """Observation-port view over the shared in-memory store."""

    def __init__(self, store: InMemoryMemoryStore) -> None:
        """Retain a shared store while avoiding protocol method-name collision."""
        self._store = store

    def save(self, observation: Observation) -> Observation:
        """Delegate observation persistence."""
        return self._store.save(observation)

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID, *, limit: int | None = None, offset: int = 0
    ) -> tuple[Observation, ...]:
        """Delegate tenant-safe observation listing."""
        return self._store.list_observations(tenant_id, workspace_id)


class InMemoryConflictReviewRepository:
    """Conflict-review port view over the shared in-memory store."""

    def __init__(self, store: InMemoryMemoryStore) -> None:
        """Retain a shared store for local review decisions."""
        self._store = store

    def save(self, decision: ConflictReviewDecision) -> ConflictReviewDecision:
        """Delegate decision persistence."""
        return self._store.save_conflict_review(decision)

    def apply_resolution(
        self,
        decision: ConflictReviewDecision,
        writes: tuple[tuple[MemoryItem, IntegrationEvent, int], ...],
    ) -> ConflictReviewDecision:
        """Delegate atomic canonical conflict resolution."""
        return self._store.apply_conflict_resolution(decision, writes)

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[ConflictReviewDecision, ...]:
        """Delegate tenant-safe review listing."""
        return self._store.list_conflict_reviews(tenant_id, workspace_id)


class InMemoryGraphRepository:
    """Graph port view over the shared in-memory store."""

    def __init__(self, store: InMemoryMemoryStore) -> None:
        """Retain shared edge storage."""
        self._store = store

    def save_edge(self, edge: MemoryEdge) -> MemoryEdge:
        """Delegate edge persistence."""
        return self._store.save_edge(edge)

    def list_neighbors(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        item_id: UUID,
        *,
        edge_type: MemoryEdgeType | None = None,
    ) -> tuple[MemoryEdge, ...]:
        """Delegate neighbor lookup."""
        return self._store.list_neighbors(
            tenant_id,
            workspace_id,
            item_id,
            edge_type=edge_type,
        )


class InMemoryCheckpointStore:
    """Thread-safe in-memory checkpoint store implementing CheckpointStore Protocol."""

    def __init__(self) -> None:
        # thread_id → list of Checkpoint ordered by revision
        self._revisions: dict[UUID, list[Checkpoint]] = {}
        self._lock = RLock()

    def save(self, checkpoint: Checkpoint) -> Checkpoint:
        """Append a new checkpoint revision unconditionally."""
        with self._lock:
            revs = self._revisions.setdefault(checkpoint.thread_id, [])
            revs.append(checkpoint)
            return checkpoint

    def save_if_head(
        self, checkpoint: Checkpoint, expected_revision: int
    ) -> Checkpoint:
        """CAS: append only when current head revision equals *expected_revision*."""

        with self._lock:
            revs = self._revisions.get(checkpoint.thread_id, [])
            tenant_revs = [r for r in revs if r.tenant_id == checkpoint.tenant_id]
            actual = tenant_revs[-1].revision if tenant_revs else 0
            if actual != expected_revision:
                raise StaleRevisionError(
                    checkpoint.thread_id, expected_revision, actual
                )
            return self.save(checkpoint)

    def get_head(self, tenant_id: UUID, thread_id: UUID) -> Checkpoint | None:
        """Return the latest revision for a thread, or None."""
        with self._lock:
            revs = self._revisions.get(thread_id, [])
            tenant_revs = [r for r in revs if r.tenant_id == tenant_id]
            return tenant_revs[-1] if tenant_revs else None

    def get_revision(
        self, tenant_id: UUID, thread_id: UUID, revision: int
    ) -> Checkpoint | None:
        """Return a specific historical revision."""
        with self._lock:
            revs = self._revisions.get(thread_id, [])
            for r in revs:
                if r.tenant_id == tenant_id and r.revision == revision:
                    return r
            return None

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID, *, limit: int | None = None, offset: int = 0
    ) -> tuple[Checkpoint, ...]:
        """List head checkpoints for all threads in a workspace."""
        with self._lock:
            heads: dict[UUID, Checkpoint] = {}
            for revs in self._revisions.values():
                for r in revs:
                    if r.tenant_id == tenant_id and r.workspace_id == workspace_id:
                        existing = heads.get(r.thread_id)
                        if existing is None or r.revision > existing.revision:
                            heads[r.thread_id] = r
            ordered = sorted(heads.values(), key=lambda c: (c.created_at, c.id))
            start = max(0, offset)
            stop = None if limit is None else start + limit
            return tuple(ordered[start:stop])

    def compact(
        self, tenant_id: UUID, thread_id: UUID, *, keep_last: int = 3
    ) -> int:
        """Delete old revisions keeping *keep_last* most recent ones."""
        with self._lock:
            revs = self._revisions.get(thread_id, [])
            tenant_revs = [r for r in revs if r.tenant_id == tenant_id]
            other_revs = [r for r in revs if r.tenant_id != tenant_id]
            if len(tenant_revs) <= keep_last:
                return 0
            removed = len(tenant_revs) - keep_last
            kept = tenant_revs[-keep_last:]
            self._revisions[thread_id] = other_revs + kept
            return removed
