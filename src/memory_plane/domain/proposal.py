"""Memory Gateway proposal domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class MemoryProposalTarget(StrEnum):
    """Requested destination for a proposed memory change."""

    AUTO = "auto"
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    TASK = "task"
    GRAPH = "graph"
    PROCEDURE = "procedure"


class MemoryProposalStatus(StrEnum):
    """Review state for a proposal submitted through Memory Gateway."""

    OPEN = "open"
    NEEDS_REVIEW = "needs_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    """Auditable proposed memory update from an agent or operator."""

    tenant_id: UUID
    workspace_id: UUID
    namespace: str
    requester: str
    target: MemoryProposalTarget
    proposal: str
    evidence: str = ""
    id: UUID = field(default_factory=uuid4)
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    confidence: float = 0.7
    importance: float = 0.5
    status: MemoryProposalStatus = MemoryProposalStatus.OPEN
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    reviewer: str | None = None
    review_reason: str = ""

    def __post_init__(self) -> None:
        """Validate proposal fields before they enter storage."""
        if not self.namespace.strip():
            raise ValueError("proposal namespace must not be empty")
        if not self.requester.strip():
            raise ValueError("proposal requester must not be empty")
        if not self.proposal.strip():
            raise ValueError("proposal text must not be empty")
        for name in ("confidence", "importance"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
