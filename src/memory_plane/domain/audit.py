"""Append-only operator audit trail domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One durable record of an operator, agent, or system action."""

    tenant_id: UUID
    workspace_id: UUID | None
    action: str
    actor: str
    actor_type: str
    resource_type: str
    resource_id: str | None = None
    status: str = "succeeded"
    metadata: dict[str, Any] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Reject records that would be useless during incident review."""
        if not self.action.strip():
            raise ValueError("audit action must not be empty")
        if not self.actor.strip():
            raise ValueError("audit actor must not be empty")
        if not self.actor_type.strip():
            raise ValueError("audit actor_type must not be empty")
        if not self.resource_type.strip():
            raise ValueError("audit resource_type must not be empty")
        if self.status not in {"succeeded", "failed", "denied"}:
            raise ValueError("audit status must be succeeded, failed or denied")

