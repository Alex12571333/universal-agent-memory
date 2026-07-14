"""Memory graph write/read service."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.graph import MemoryEdge, MemoryEdgeType
from memory_plane.ports.repositories import GraphRepository, MemoryLedger


class GraphService:
    """Manage typed relationships between memory items."""

    def __init__(self, ledger: MemoryLedger, graph: GraphRepository) -> None:
        """Bind graph writes to canonical memory existence checks."""
        self._ledger = ledger
        self._graph = graph

    def link(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        src_id: UUID,
        dst_id: UUID,
        edge_type: MemoryEdgeType,
        weight: float = 1.0,
        provenance_item_id: UUID | None = None,
        audit_event: AuditEvent | None = None,
    ) -> MemoryEdge:
        """Create a graph edge only when both endpoint memories exist."""
        src = self._ledger.get(tenant_id, src_id)
        dst = self._ledger.get(tenant_id, dst_id)
        if src is None or dst is None:
            raise KeyError("memory edge endpoint not found")
        if src.workspace_id != workspace_id or dst.workspace_id != workspace_id:
            raise ValueError("memory edge endpoints must share workspace")
        edge = MemoryEdge(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            src_id=src_id,
            dst_id=dst_id,
            edge_type=edge_type,
            weight=weight,
            provenance_item_id=provenance_item_id,
        )
        return self._graph.save_edge(edge, audit_event=audit_event)

    def neighbors(
        self,
        *,
        tenant_id: UUID,
        workspace_id: UUID,
        item_id: UUID,
        edge_type: MemoryEdgeType | None = None,
        after_created_at: datetime | None = None,
        after_edge_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[MemoryEdge, ...]:
        """List incoming and outgoing edges for one memory item."""
        return self._graph.list_neighbors(
            tenant_id,
            workspace_id,
            item_id,
            edge_type=edge_type,
            after_created_at=after_created_at,
            after_edge_id=after_edge_id,
            limit=limit,
        )
