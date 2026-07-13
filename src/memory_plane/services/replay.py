"""Privacy-preserving explanation of a persisted recall operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from memory_plane.domain.audit import AuditEvent
from memory_plane.ports.repositories import MemoryLedger
from memory_plane.services.audit import AuditLogService


@dataclass(frozen=True, slots=True)
class ReplayMemoryReference:
    """A selected canonical memory reference without its text payload."""

    item_id: UUID
    layer: str
    status: str
    revision: int


@dataclass(frozen=True, slots=True)
class RecallReplay:
    """Redacted, deterministic rendering of one durable recall audit event."""

    audit_event: AuditEvent
    operation: str
    query_sha256: str
    query_chars: int
    candidate_count: int
    sources_used: tuple[str, ...]
    index_stale: bool
    context_budget_tokens: int
    context_used_tokens: int
    trace_ids: tuple[UUID, ...]
    references: tuple[ReplayMemoryReference, ...]


class RecallReplayService:
    """Rebuild one recall explanation using audit metadata and canonical IDs only."""

    def __init__(self, audit: AuditLogService, ledger: MemoryLedger) -> None:
        self._audit = audit
        self._ledger = ledger

    def get(self, tenant_id: UUID, workspace_id: UUID, audit_event_id: UUID) -> RecallReplay:
        """Return a replay only when its event belongs to the requested scope."""
        event = self._audit.get_event(tenant_id, audit_event_id)
        if event is None or event.workspace_id != workspace_id:
            raise KeyError("recall replay not found")
        if event.action != "memory.recall" or event.resource_type != "memory_recall":
            raise ValueError("audit event is not a recall replay")
        metadata = event.metadata
        trace_ids = _uuid_tuple(metadata.get("trace_ids"))
        references: list[ReplayMemoryReference] = []
        for item_id in trace_ids:
            item = self._ledger.get(tenant_id, item_id)
            if item is None or item.workspace_id != workspace_id:
                continue
            references.append(
                ReplayMemoryReference(
                    item_id=item.id,
                    layer=item.layer.value,
                    status=item.status.value,
                    revision=item.revision,
                )
            )
        return RecallReplay(
            audit_event=event,
            operation=_bounded_text(metadata.get("operation"), default="unknown"),
            query_sha256=_bounded_text(metadata.get("query_sha256"), default=""),
            query_chars=_bounded_int(metadata.get("query_chars")),
            candidate_count=_bounded_int(metadata.get("candidate_count")),
            sources_used=_text_tuple(metadata.get("sources_used")),
            index_stale=bool(metadata.get("index_stale", False)),
            context_budget_tokens=_bounded_int(metadata.get("context_budget_tokens")),
            context_used_tokens=_bounded_int(metadata.get("context_used_tokens")),
            trace_ids=trace_ids,
            references=tuple(references),
        )


def _uuid_tuple(value: Any) -> tuple[UUID, ...]:
    """Parse bounded UUID metadata defensively without accepting arbitrary payloads."""
    if not isinstance(value, list):
        return ()
    parsed: list[UUID] = []
    for row in value[:1000]:
        try:
            parsed.append(UUID(str(row)))
        except (TypeError, ValueError):
            continue
    return tuple(parsed)


def _text_tuple(value: Any) -> tuple[str, ...]:
    """Return a bounded set of small source names from audit metadata."""
    if not isinstance(value, list):
        return ()
    return tuple(_bounded_text(row, default="unknown") for row in value[:32])


def _bounded_text(value: Any, *, default: str) -> str:
    """Keep replay strings intentionally small and presentation-safe."""
    if not isinstance(value, str):
        return default
    return value[:256]


def _bounded_int(value: Any) -> int:
    """Decode non-negative metrics without trusting audit JSON types."""
    try:
        return max(0, min(int(value), 1_000_000))
    except (TypeError, ValueError):
        return 0
