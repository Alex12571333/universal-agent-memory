"""Application service for durable audit events."""

from __future__ import annotations

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

    def list_events(
        self,
        tenant_id: UUID,
        *,
        workspace_id: UUID | None = None,
        action: str | None = None,
        resource_type: str | None = None,
        limit: int = 100,
    ) -> tuple[AuditEvent, ...]:
        """List recent audit events under the tenant boundary."""
        return self._repository.list_audit_events(
            tenant_id,
            workspace_id=workspace_id,
            action=action,
            resource_type=resource_type,
            limit=max(1, min(int(limit), 500)),
        )

