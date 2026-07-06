"""Conflict-review domain objects for human-governed memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID


class ConflictReviewStatus(StrEnum):
    """Human review state for a conflict case."""

    UNRESOLVED = "unresolved"
    ACCEPTED = "accepted"
    OVERRIDDEN = "overridden"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    """One competing value in a conflict case."""

    value: str
    status: str
    evidence_ids: tuple[UUID, ...]
    confidence: float
    latest_created_at: datetime


@dataclass(frozen=True, slots=True)
class ConflictReviewDecision:
    """Persisted human/operator decision for a conflict case."""

    tenant_id: UUID
    workspace_id: UUID
    case_id: UUID
    status: ConflictReviewStatus
    winner_value: str | None = None
    reason: str = ""
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ConflictCase:
    """Inspectable case produced from contradictory memory evidence."""

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    subject: str
    predicate: str
    candidates: tuple[ConflictCandidate, ...]
    suggested_winner_value: str
    suggested_reason: str
    review: ConflictReviewDecision | None = None

    @property
    def review_status(self) -> ConflictReviewStatus:
        """Return the persisted review state, or unresolved by default."""
        return self.review.status if self.review else ConflictReviewStatus.UNRESOLVED
