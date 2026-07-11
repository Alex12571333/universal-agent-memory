"""Stable agent and thread identities used by durable memory foreign keys."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """One workspace pinned to its tenant boundary."""

    id: UUID
    tenant_id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """One agent identity pinned to exactly one tenant/workspace boundary."""

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    name: str
    role: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ThreadIdentity:
    """One conversation/run identity pinned to an optional owner agent."""

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    owner_agent_id: UUID | None
    status: str = "active"
