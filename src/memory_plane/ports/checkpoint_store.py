"""Infrastructure port for working-memory checkpoint persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.checkpoint import Checkpoint


class CheckpointStore(Protocol):
    """Revisioned checkpoint storage with compare-and-swap semantics."""

    def save(self, checkpoint: Checkpoint, audit_event: AuditEvent | None = None) -> Checkpoint:
        """Append a new checkpoint revision unconditionally."""
        ...

    def save_if_head(
        self,
        checkpoint: Checkpoint,
        expected_revision: int,
        audit_event: AuditEvent | None = None,
    ) -> Checkpoint:
        """CAS: append only if current head revision == expected_revision.

        Raises StaleRevisionError when the head has already advanced.
        """
        ...

    def get_head(
        self, tenant_id: UUID, thread_id: UUID
    ) -> Checkpoint | None:
        """Return the latest revision for a thread, or None."""
        ...

    def get_revision(
        self, tenant_id: UUID, thread_id: UUID, revision: int
    ) -> Checkpoint | None:
        """Return a specific historical revision."""
        ...

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID
    ) -> tuple[Checkpoint, ...]:
        """List head checkpoints for all threads in a workspace."""
        ...

    def compact(
        self, tenant_id: UUID, thread_id: UUID, *, keep_last: int = 3
    ) -> int:
        """Delete old revisions keeping the most recent *keep_last*. Returns count deleted."""
        ...
