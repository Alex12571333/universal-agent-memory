"""Transport-neutral application contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    Provenance,
)


@dataclass(frozen=True, slots=True)
class IngestDocumentCommand:
    """Request to normalize and enqueue one text document."""

    tenant_id: UUID
    workspace_id: UUID
    text: str
    origin_uri: str
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    labels: tuple[str, ...] = ()
    chunk_size_chars: int = 2400
    chunk_overlap_chars: int = 240
    document_checksum: str | None = None
    source_kind: str = "document"
    extraction_version: str = "text-chunker-v1"

    def __post_init__(self) -> None:
        """Validate deterministic chunking parameters."""
        if not self.text.strip():
            raise ValueError("document text must not be empty")
        if self.chunk_size_chars < 256:
            raise ValueError("chunk_size_chars must be at least 256")
        if not 0 <= self.chunk_overlap_chars < self.chunk_size_chars:
            raise ValueError("chunk overlap must be >= 0 and smaller than chunk size")


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Canonical memory identities created from a document."""

    document_checksum: str
    memory_ids: tuple[UUID, ...]
    created_count: int


@dataclass(frozen=True, slots=True)
class RetainCommand:
    """Request to append one canonical memory atom."""

    tenant_id: UUID
    workspace_id: UUID
    layer: MemoryLayer
    scope: MemoryScope
    kind: str
    text: str
    provenance: Provenance
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    labels: tuple[str, ...] = ()
    importance: float = 0.5
    confidence: float = 0.7
    metadata: dict[str, Any] = field(default_factory=dict)
    status: MemoryStatus = MemoryStatus.ACTIVE
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class RetainResult:
    """Result of the transactional retain operation."""

    item: MemoryItem
    created: bool
    queued_event_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class SupersedeMemoryCommand:
    """CAS request to append a replacement for the current memory head."""

    tenant_id: UUID
    item_id: UUID
    replacement_text: str
    expected_revision: int
    confidence: float | None = None
    status: MemoryStatus | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        """Reject stale-write requests that cannot be evaluated safely."""
        if not self.replacement_text.strip():
            raise ValueError("replacement text must not be empty")
        if self.expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RecallQuery:
    """A tenant-bound, policy-ready retrieval request."""

    tenant_id: UUID
    workspace_id: UUID
    text: str
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    layers: tuple[MemoryLayer, ...] = ()
    labels: tuple[str, ...] = ()
    valid_at: datetime | None = None
    top_k: int = 12
    minimum_score: float = 0.0

    def __post_init__(self) -> None:
        """Keep fan-out and API resource use bounded."""
        if not self.text.strip():
            raise ValueError("recall query must not be empty")
        if not 1 <= self.top_k <= 1000:
            raise ValueError("top_k must be between 1 and 1000")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One candidate and its normalized retrieval signals."""

    item: MemoryItem
    source: str
    semantic: float = 0.0
    lexical: float = 0.0
    entity: float = 0.0
    recency: float = 0.0
    trust: float = 0.0
    final_score: float = 0.0


@dataclass(frozen=True, slots=True)
class RecallResult:
    """Ranked retrieval output with inspectable source diagnostics."""

    candidates: tuple[Candidate, ...]
    sources_used: tuple[str, ...]
    index_stale: bool = False


@dataclass(frozen=True, slots=True)
class ContextRecipe:
    """Per-operation memory selection and token allocation policy."""

    operation: str
    budget_tokens: int
    layer_order: tuple[MemoryLayer, ...]
    per_layer_limit: dict[MemoryLayer, int] = field(default_factory=dict)
    always_include: tuple[MemoryLayer, ...] = (MemoryLayer.CORE, MemoryLayer.WORKING)

    def __post_init__(self) -> None:
        """Reject recipes that cannot produce a useful package."""
        if self.budget_tokens < 128:
            raise ValueError("context budget must be at least 128 tokens")
