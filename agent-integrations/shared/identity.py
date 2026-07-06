"""Stable identity helpers for native agent adapters."""

from __future__ import annotations

import os
from uuid import NAMESPACE_URL, UUID, uuid5


def resolve_uuid(env_name: str, *, fallback: str) -> UUID:
    """Resolve a UUID from env, or derive a stable UUIDv5 fallback."""
    raw = os.getenv(env_name)
    if raw:
        return UUID(raw)
    return uuid5(NAMESPACE_URL, f"universal-agent-memory:{fallback}")


def resolve_tenant_id(*, fallback: str = "default-tenant") -> UUID:
    """Resolve the tenant identity used by native adapters."""
    return resolve_uuid("UAM_TENANT_ID", fallback=f"tenant:{fallback}")


def resolve_workspace_id(*, fallback: str) -> UUID:
    """Resolve the workspace identity used by native adapters."""
    return resolve_uuid("UAM_WORKSPACE_ID", fallback=f"workspace:{fallback}")


def resolve_agent_id(*, fallback: str) -> UUID:
    """Resolve the agent identity used by native adapters."""
    return resolve_uuid("UAM_AGENT_ID", fallback=f"agent:{fallback}")
