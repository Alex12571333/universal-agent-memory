"""Synchronous, narrow and transactional memory write path."""

from __future__ import annotations

from dataclasses import replace

from memory_plane.contracts.dto import RetainCommand, RetainResult, SupersedeMemoryCommand
from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.models import MemoryItem
from memory_plane.ports.repositories import RetentionStore
from memory_plane.services.privacy import PrivacyGuard


class RetentionService:
    """Append canonical memory and enqueue derived processing."""

    def __init__(
        self,
        store: RetentionStore,
        privacy: PrivacyGuard | None = None,
    ) -> None:
        """Bind the service to one atomic ledger/outbox boundary."""
        self._store = store
        self._privacy = privacy or PrivacyGuard.from_env()

    def retain(self, command: RetainCommand) -> RetainResult:
        """Validate, append and emit one `memory.retained.v1` event.

        Extraction, embedding, graph updates and consolidation deliberately stay
        off this hot path. Idempotency is delegated to the ledger transaction.
        """
        decision = self._privacy.apply(command.text)
        item = MemoryItem(
            tenant_id=command.tenant_id,
            workspace_id=command.workspace_id,
            agent_id=command.agent_id,
            thread_id=command.thread_id,
            layer=command.layer,
            scope=command.scope,
            kind=command.kind,
            text=decision.text,
            labels=command.labels,
            metadata={**command.metadata, **decision.metadata},
            status=command.status,
            provenance=command.provenance,
            importance=command.importance,
            confidence=command.confidence,
        )
        event = IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=item.tenant_id,
            workspace_id=item.workspace_id,
            correlation_id=item.id,
            payload={
                "memory_id": str(item.id),
                "layer": item.layer.value,
                "jobs": ["embed", "dedupe", "graph", "reflect"],
            },
        )
        stored, created = self._store.retain(item, event, command.idempotency_key)
        if not created:
            return RetainResult(item=stored, created=False, queued_event_ids=())

        return RetainResult(item=stored, created=True, queued_event_ids=(event.id,))

    def supersede(self, command: SupersedeMemoryCommand) -> RetainResult:
        """Append a replacement item only if the caller observed the current head."""
        current = self._store.get(command.tenant_id, command.item_id)
        if current is None:
            raise KeyError("memory item not found")
        decision = self._privacy.apply(command.replacement_text)
        replacement = current.supersede(
            decision.text,
            confidence=command.confidence,
        )
        replacement = replace(
            replacement,
            metadata={**replacement.metadata, **decision.metadata},
        )
        event = IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=replacement.tenant_id,
            workspace_id=replacement.workspace_id,
            correlation_id=replacement.id,
            payload={
                "memory_id": str(replacement.id),
                "supersedes_id": str(current.id),
                "revision": replacement.revision,
                "layer": replacement.layer.value,
                "jobs": ["embed", "dedupe", "graph", "reflect"],
            },
        )
        stored, created = self._store.supersede_if_current(
            replacement,
            event,
            expected_revision=command.expected_revision,
            idempotency_key=command.idempotency_key,
        )
        return RetainResult(
            item=stored,
            created=created,
            queued_event_ids=(event.id,) if created else (),
        )
