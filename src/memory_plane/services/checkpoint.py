"""Working-memory checkpoint application service."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from memory_plane.domain.checkpoint import Checkpoint, StaleRevisionError
from memory_plane.ports.checkpoint_store import CheckpointStore


class CheckpointService:
    """Coordinate checkpoint lifecycle with compare-and-swap guarantees."""

    def __init__(self, store: CheckpointStore) -> None:
        self._store = store

    def save(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        thread_id: UUID,
        state: dict[str, Any],
    ) -> Checkpoint:
        """Create or append a new checkpoint revision for *thread_id*.

        If a checkpoint already exists the new revision number is auto-
        incremented and protected by CAS against concurrent writers.
        """
        head = self._store.get_head(tenant_id, thread_id)
        new_revision = (head.revision + 1) if head else 1
        checkpoint = Checkpoint(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            revision=new_revision,
            state=state,
        )
        return self._store.save_if_head(checkpoint, head.revision if head else 0)

    def update(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        thread_id: UUID,
        state: dict[str, Any],
        expected_revision: int,
    ) -> Checkpoint:
        """CAS update: create the next revision only when *expected_revision* is head.

        Raises ``StaleRevisionError`` if the head has already moved past
        *expected_revision*, preventing lost updates.
        """
        head = self._store.get_head(tenant_id, thread_id)
        if head is None:
            raise StaleRevisionError(thread_id, expected_revision, None)
        checkpoint = Checkpoint(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
            revision=head.revision + 1,
            state=state,
        )
        return self._store.save_if_head(checkpoint, expected_revision)

    def restore(self, *, tenant_id: UUID, thread_id: UUID) -> Checkpoint | None:
        """Return the latest checkpoint for a thread."""
        return self._store.get_head(tenant_id, thread_id)

    def restore_revision(
        self, *, tenant_id: UUID, thread_id: UUID, revision: int
    ) -> Checkpoint | None:
        """Return a specific historical checkpoint revision."""
        return self._store.get_revision(tenant_id, thread_id, revision)

    def list_for_workspace(
        self, tenant_id: UUID, workspace_id: UUID, *, limit: int | None = None, offset: int = 0
    ) -> tuple[Checkpoint, ...]:
        """List head checkpoints for all threads in a workspace."""
        return self._store.list_for_workspace(tenant_id, workspace_id, limit=limit, offset=offset)

    def compact(
        self, *, tenant_id: UUID, thread_id: UUID, keep_last: int = 3
    ) -> int:
        """Delete old revisions keeping *keep_last* most recent ones."""
        return self._store.compact(tenant_id, thread_id, keep_last=keep_last)
