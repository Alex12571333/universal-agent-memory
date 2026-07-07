"""Canonical domain objects.

This module has no framework or storage dependencies. It is the stable vocabulary
shared by API, workers and adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class MemoryLayer(StrEnum):
    """A cognitive/operational tier used for storage and retrieval policies."""

    WORKING = "working"
    CORE = "core"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    SOCIAL = "social"
    REFLECTION = "reflection"
    ERROR = "error"


class MemoryScope(StrEnum):
    """Visibility boundary; authorization still belongs to the policy layer."""

    PRIVATE = "private"
    THREAD = "thread"
    TEAM = "team"
    WORKSPACE = "workspace"
    ORGANIZATION = "organization"


class MemoryStatus(StrEnum):
    """Lifecycle state used by retrieval and review policies."""

    ACTIVE = "active"
    STALE = "stale"
    DEPRECATED = "deprecated"
    DISPUTED = "disputed"
    HYPOTHESIS = "hypothesis"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    PINNED = "pinned"


class MemoryRevisionConflictError(Exception):
    """CAS conflict for append-only MemoryItem supersede operations."""

    def __init__(
        self, item_id: UUID, expected_revision: int, actual_revision: int | None
    ) -> None:
        """Describe the stale write attempt without adapter-specific details."""
        self.item_id = item_id
        self.expected = expected_revision
        self.actual = actual_revision
        super().__init__(
            f"stale revision for memory {item_id}: "
            f"expected {expected_revision}, actual {actual_revision}"
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    """Trace from derived memory back to immutable source evidence."""

    source_kind: str
    origin_uri: str | None = None
    object_key: str | None = None
    checksum_sha256: str | None = None
    quote: str | None = None
    extraction_version: str = "manual-v1"


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """Append-only canonical memory atom."""

    tenant_id: UUID
    workspace_id: UUID
    layer: MemoryLayer
    scope: MemoryScope
    kind: str
    text: str
    provenance: Provenance
    id: UUID = field(default_factory=uuid4)
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    status: MemoryStatus = MemoryStatus.ACTIVE
    importance: float = 0.5
    salience: float = 0.5
    confidence: float = 0.7
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    revision: int = 1
    supersedes_id: UUID | None = None

    def __post_init__(self) -> None:
        """Validate cross-field invariants at the domain boundary."""
        if not self.text.strip():
            raise ValueError("memory text must not be empty")
        for name in ("importance", "salience", "confidence"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.scope == MemoryScope.THREAD and self.thread_id is None:
            raise ValueError("thread-scoped memory requires thread_id")
        if self.status == MemoryStatus.PINNED and self.layer != MemoryLayer.CORE:
            raise ValueError("pinned memory must live in the core layer")

    def is_valid_at(self, moment: datetime) -> bool:
        """Return whether this item was valid at a requested point in time."""
        return not (
            (self.valid_from is not None and moment < self.valid_from)
            or (self.valid_to is not None and moment >= self.valid_to)
        )

    def supersede(self, replacement_text: str, *, confidence: float | None = None) -> MemoryItem:
        """Create a new revision; never mutate or erase the old item."""
        return replace(
            self,
            id=uuid4(),
            text=replacement_text,
            confidence=self.confidence if confidence is None else confidence,
            created_at=datetime.now(UTC),
            revision=self.revision + 1,
            supersedes_id=self.id,
        )


@dataclass(frozen=True, slots=True)
class Observation:
    """Evidence-grounded consolidated belief produced by reflection."""

    tenant_id: UUID
    workspace_id: UUID
    summary: str
    evidence_ids: tuple[UUID, ...]
    id: UUID = field(default_factory=uuid4)
    confidence: float = 0.7
    stale: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Reject observations that cannot be audited."""
        if not self.summary.strip():
            raise ValueError("observation summary must not be empty")
        if not self.evidence_ids:
            raise ValueError("observation requires at least one evidence item")


@dataclass(frozen=True, slots=True)
class ContextSection:
    """One ordered section of a compiled context package."""

    name: str
    items: tuple[MemoryItem, ...]
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ContextPackage:
    """Budgeted, traceable context delivered to one agent operation."""

    operation: str
    sections: tuple[ContextSection, ...]
    budget_tokens: int
    used_tokens: int
    trace_ids: tuple[UUID, ...]

    def render_markdown(self) -> str:
        """Render a deterministic human/LLM-readable representation."""
        chunks: list[str] = []
        for section in self.sections:
            chunks.append(f"## {section.name}")
            chunks.extend(f"- {item.text}" for item in section.items)
        return "\n".join(chunks)
