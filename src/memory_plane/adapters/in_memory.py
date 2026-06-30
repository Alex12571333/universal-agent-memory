"""Deterministic development adapter implementing all core ports."""

from __future__ import annotations

import re
from threading import RLock
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import MemoryItem, MemoryLayer, Observation

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

    def append(self, item: MemoryItem, idempotency_key: str | None = None) -> tuple[MemoryItem, bool]:
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

    def get(self, tenant_id: UUID, item_id: UUID) -> MemoryItem | None:
        """Return an item only when its tenant matches exactly."""
        item = self._items.get(item_id)
        return item if item is not None and item.tenant_id == tenant_id else None

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
            if query.thread_id and item.scope.value == "thread" and item.thread_id != query.thread_id:
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
