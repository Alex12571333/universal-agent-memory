"""Synchronous, narrow and transactional memory write path."""

from __future__ import annotations

from memory_plane.contracts.dto import RetainCommand, RetainResult
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import MemoryItem
from memory_plane.ports.repositories import EventPublisher, MemoryLedger


class RetentionService:
    """Append canonical memory and enqueue derived processing."""

    def __init__(self, ledger: MemoryLedger, events: EventPublisher) -> None:
        """Bind the service to a transactional ledger and outbox."""
        self._ledger = ledger
        self._events = events

    def retain(self, command: RetainCommand) -> RetainResult:
        """Validate, append and emit one `memory.retained.v1` event.

        Extraction, embedding, graph updates and consolidation deliberately stay
        off this hot path. Idempotency is delegated to the ledger transaction.
        """
        item = MemoryItem(
            tenant_id=command.tenant_id,
            workspace_id=command.workspace_id,
            agent_id=command.agent_id,
            thread_id=command.thread_id,
            layer=command.layer,
            scope=command.scope,
            kind=command.kind,
            text=command.text,
            labels=command.labels,
            provenance=command.provenance,
            importance=command.importance,
            confidence=command.confidence,
        )
        stored, created = self._ledger.append(item, command.idempotency_key)
        if not created:
            return RetainResult(item=stored, created=False, queued_event_ids=())

        event = IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=stored.tenant_id,
            workspace_id=stored.workspace_id,
            correlation_id=stored.id,
            payload={
                "memory_id": str(stored.id),
                "layer": stored.layer.value,
                "jobs": ["embed", "dedupe", "graph", "reflect"],
            },
        )
        self._events.publish(event)
        return RetainResult(item=stored, created=True, queued_event_ids=(event.id,))
