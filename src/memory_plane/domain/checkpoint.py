"""Working-memory checkpoint domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Revisioned working-memory snapshot for one thread.

    Each save appends a new revision. CAS semantics prevent lost updates:
    the caller must present the expected head revision to update.
    """

    tenant_id: UUID
    workspace_id: UUID
    thread_id: UUID
    revision: int
    state: dict[str, Any]
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Validate cross-field invariants at the domain boundary."""
        if self.revision < 1:
            raise ValueError("checkpoint revision must be positive")


class StaleRevisionError(Exception):
    """CAS conflict: expected revision doesn't match current head."""

    def __init__(
        self, thread_id: UUID, expected: int, actual: int | None
    ) -> None:
        self.thread_id = thread_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stale revision for thread {thread_id}: "
            f"expected {expected}, actual {actual}"
        )
