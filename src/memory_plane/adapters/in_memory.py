"""Deterministic development adapter implementing all core ports."""

from __future__ import annotations

import re
from threading import RLock
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryRevisionConflictError,
    MemoryScope,
    Observation,
)

_WORD = re.compile(r"\w+", re.UNICODE)


class InMemoryMemoryStore:
    """Thread-safe fake ledger, outbox, observation store and lexical source."""

    def __init__(self) -> None:
        """Initialize isolated mutable state for one test or local process."""
        self._items: dict[UUID, MemoryItem] = {}
        self._idempotency: dict[tuple[UUID, str], UUID] = {}
        self._observations: dict[UUID, Observation] = {}
        self.events: list[IntegrationEvent] = []
        self._lock = RLock()

    @property
    def name(self) -> str:
        """Return the stable retrieval diagnostic name."""
        return "sql_lexical"

    def append(
        self, item: MemoryItem, idempotency_key: str | None = None
    ) -> tuple[MemoryItem, bool]:
        """Atomically append or return an idempotent prior result."""
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
    ) -> tuple[MemoryItem, ...]:
        """List workspace items in creation order with optional layer filtering."""
        rows = [
            item
            for item in self._items.values()
            if item.tenant_id == tenant_id
            and item.workspace_id == workspace_id
            and (not layers or item.layer in layers)
        ]
        return tuple(sorted(rows, key=lambda item: item.created_at))

    def search(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """Provide portable lexical candidates and strict metadata filtering."""
        query_terms = self._terms(query.text)
        rows: list[Candidate] = []
        for item in self.list_for_workspace(
            query.tenant_id, query.workspace_id, layers=query.layers
        ):
            if item.scope == MemoryScope.THREAD and item.thread_id != query.thread_id:
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
            }

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
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[Observation, ...]:
        """Delegate tenant-safe observation listing."""
        return self._store.list_observations(tenant_id, workspace_id)


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
            actual = tenant_revs[-1].revision if tenant_revs else None
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
        self, tenant_id: UUID, workspace_id: UUID
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
            return tuple(
                v for v in sorted(heads.values(), key=lambda c: c.created_at)
            )

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
