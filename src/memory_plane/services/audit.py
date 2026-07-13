"""Application service for durable audit events."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from memory_plane.domain.audit import AuditEvent
from memory_plane.ports.repositories import AuditRepository


class AuditLogService:
    """Append-only audit log facade used by API and workers."""

    def __init__(self, repository: AuditRepository) -> None:
        """Retain a repository without opening external resources."""
        self._repository = repository

    def record(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID | None,
        action: str,
        actor: str,
        actor_type: str,
        resource_type: str,
        resource_id: str | None = None,
        status: str = "succeeded",
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Persist one audit event and return the stored row."""
        event = AuditEvent(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            action=action,
            actor=actor,
            actor_type=actor_type,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            metadata=metadata or {},
        )
        return self._repository.append_audit_event(event)

    def get_event(self, tenant_id: UUID, event_id: UUID) -> AuditEvent | None:
        """Load one event for a tenant-scoped explanation endpoint."""
        return self._repository.get_audit_event(tenant_id, event_id)

    def list_events(
        self,
        tenant_id: UUID,
        *,
        workspace_id: UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        before_event_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        """List recent audit events under the tenant boundary."""
        return self._repository.list_audit_events(
            tenant_id,
            workspace_id=workspace_id,
            action=action,
            resource_type=resource_type,
            created_after=created_after,
            created_before=created_before,
            before_event_id=before_event_id,
            limit=max(1, min(int(limit), 500)),
        )

    def prune_events(
        self,
        tenant_id: UUID,
        *,
        created_before: datetime,
        workspace_id: UUID | None = None,
        limit: int = 500,
    ) -> int:
        """Prune old events after a signed external export has been verified."""
        return self._repository.prune_audit_events(
            tenant_id,
            workspace_id=workspace_id,
            created_before=created_before,
            limit=max(1, min(int(limit), 500)),
        )
