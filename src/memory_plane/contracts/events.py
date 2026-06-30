"""Versioned event envelope used by outbox and message brokers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class IntegrationEvent:
    """Self-contained, idempotently consumable integration event."""

    name: str
    tenant_id: UUID
    workspace_id: UUID
    payload: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    correlation_id: UUID | None = None

    def __post_init__(self) -> None:
        """Require explicit event schema versions in the event name."""
        if not self.name.rsplit(".", 1)[-1].startswith("v"):
            raise ValueError("event name must end in a version, e.g. memory.retained.v1")


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    """One leased outbox event and its current delivery attempt."""

    event: IntegrationEvent
    attempts: int

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("claimed event attempts must be positive")


class ConsumerClaim(StrEnum):
    """Result of acquiring one event for one named consumer."""

    ACQUIRED = "acquired"
    COMPLETED = "completed"
    BUSY = "busy"
