"""Operator-controlled provisioning for durable agent/thread identities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from memory_plane.domain.identity import AgentIdentity, ThreadIdentity, WorkspaceIdentity
from memory_plane.ports.repositories import IdentityRegistry


@dataclass(frozen=True, slots=True)
class ProvisionIdentityCommand:
    """Idempotently create or update one agent and optional owned thread."""

    tenant_id: UUID
    workspace_id: UUID
    agent_id: UUID
    agent_name: str
    agent_role: str
    agent_config: dict[str, Any] = field(default_factory=dict)
    thread_id: UUID | None = None
    thread_status: str = "active"


@dataclass(frozen=True, slots=True)
class ProvisionWorkspaceCommand:
    """Operator request to idempotently register a tenant-owned workspace."""

    tenant_id: UUID
    workspace_id: UUID
    workspace_name: str


class IdentityProvisioningService:
    """Validate identity metadata before crossing the atomic storage boundary."""

    _THREAD_STATUSES = frozenset({"active", "closed", "archived"})

    def __init__(self, registry: IdentityRegistry) -> None:
        self.registry = registry

    def provision(
        self,
        command: ProvisionIdentityCommand,
    ) -> tuple[AgentIdentity, ThreadIdentity | None]:
        name = command.agent_name.strip()
        role = command.agent_role.strip()
        status = command.thread_status.strip().lower()
        if not name:
            raise ValueError("agent_name must not be empty")
        if not role:
            raise ValueError("agent_role must not be empty")
        if len(name) > 160 or len(role) > 80:
            raise ValueError("agent identity metadata is too long")
        if status not in self._THREAD_STATUSES:
            raise ValueError("thread_status must be active, closed, or archived")
        return self.registry.provision_agent_thread(
            AgentIdentity(
                id=command.agent_id,
                tenant_id=command.tenant_id,
                workspace_id=command.workspace_id,
                name=name,
                role=role,
                config=dict(command.agent_config),
            ),
            thread_id=command.thread_id,
            thread_status=status,
        )

    def provision_workspace(self, command: ProvisionWorkspaceCommand) -> WorkspaceIdentity:
        """Validate workspace metadata before registering its durable scope."""
        name = command.workspace_name.strip()
        if not name:
            raise ValueError("workspace_name must not be empty")
        if len(name) > 160:
            raise ValueError("workspace_name is too long")
        return self.registry.provision_workspace(
            WorkspaceIdentity(
                id=command.workspace_id,
                tenant_id=command.tenant_id,
                name=name,
            )
        )
