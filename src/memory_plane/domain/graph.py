"""Memory graph edge domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class MemoryEdgeType(StrEnum):
    """Supported relationship types between memory items."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DERIVED_FROM = "derived_from"
    SAME_ENTITY = "same_entity"
    CAUSED_BY = "caused_by"
    OWNED_BY_AGENT = "owned_by_agent"
    FROM_THREAD = "from_thread"
    SUPERSEDES = "supersedes"


@dataclass(frozen=True, slots=True)
class MemoryEdge:
    """Typed, auditable relationship between two memory items."""

    tenant_id: UUID
    workspace_id: UUID
    src_id: UUID
    dst_id: UUID
    edge_type: MemoryEdgeType
    id: UUID = field(default_factory=uuid4)
    weight: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    provenance_item_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate graph edge invariants."""
        if self.src_id == self.dst_id:
            raise ValueError("memory edge cannot point to itself")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError("edge weight must be between 0 and 1")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
