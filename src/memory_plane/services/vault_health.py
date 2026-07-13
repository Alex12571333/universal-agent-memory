"""Deterministic, read-only integrity checks for a projected memory vault."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from memory_plane.domain.graph import MemoryEdge
from memory_plane.domain.models import MemoryItem, MemoryStatus, Observation
from memory_plane.ports.repositories import (
    GraphRepository,
    MemoryLedger,
    ObservationRepository,
)


@dataclass(frozen=True, slots=True)
class VaultHealthIssue:
    """One tenant-scoped, non-mutating vault-health finding."""

    severity: str
    code: str
    message: str
    item_id: UUID | None = None
    edge_id: UUID | None = None
    observation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class VaultHealthReport:
    """A deterministic health report for one canonical workspace projection."""

    tenant_id: UUID
    workspace_id: UUID
    memory_count: int
    edge_count: int
    observation_count: int
    recallable_head_count: int
    unlinked_head_count: int
    issues: tuple[VaultHealthIssue, ...]

    @property
    def error_count(self) -> int:
        """Count integrity failures that require operator investigation."""
        return sum(issue.severity == "error" for issue in self.issues)

    @property
    def warning_count(self) -> int:
        """Count diagnostics that do not invalidate canonical memory."""
        return sum(issue.severity == "warning" for issue in self.issues)

    @property
    def healthy(self) -> bool:
        """A vault is healthy when no canonical-reference error is present."""
        return self.error_count == 0


class VaultHealthService:
    """Check graph/evidence/revision references without calling models or mutating data."""

    def __init__(
        self,
        ledger: MemoryLedger,
        observations: ObservationRepository,
        graph: GraphRepository,
    ) -> None:
        self._ledger = ledger
        self._observations = observations
        self._graph = graph

    def inspect(self, tenant_id: UUID, workspace_id: UUID) -> VaultHealthReport:
        """Produce a tenant-scoped report from canonical records only."""
        items = self._ledger.list_for_workspace(tenant_id, workspace_id)
        observations = self._observations.list_for_workspace(tenant_id, workspace_id)
        edges = self._graph.list_for_workspace(tenant_id, workspace_id)
        items_by_id = {item.id: item for item in items}
        issues: list[VaultHealthIssue] = []

        self._check_revision_chains(items_by_id, issues)
        linked_item_ids = self._check_edges(items_by_id, edges, issues)
        linked_item_ids.update(self._check_observations(items_by_id, observations, issues))

        heads = tuple(
            item
            for item in items
            if self._ledger.is_recallable_head(tenant_id, item.id)
            and item.status not in {MemoryStatus.ARCHIVED, MemoryStatus.REJECTED}
        )
        unlinked_heads = tuple(item for item in heads if item.id not in linked_item_ids)
        issues.extend(
            VaultHealthIssue(
                severity="warning",
                code="unlinked_memory_head",
                message=(
                    "recallable memory head has no typed graph edge or active "
                    "observation evidence"
                ),
                item_id=item.id,
            )
            for item in unlinked_heads
        )
        issues.sort(
            key=lambda issue: (
                0 if issue.severity == "error" else 1,
                issue.code,
                str(issue.item_id or issue.edge_id or issue.observation_id or ""),
            )
        )
        return VaultHealthReport(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            memory_count=len(items),
            edge_count=len(edges),
            observation_count=len(observations),
            recallable_head_count=len(heads),
            unlinked_head_count=len(unlinked_heads),
            issues=tuple(issues),
        )

    @staticmethod
    def _check_revision_chains(
        items_by_id: dict[UUID, MemoryItem], issues: list[VaultHealthIssue]
    ) -> None:
        for item in items_by_id.values():
            if item.supersedes_id is None:
                continue
            parent = items_by_id.get(item.supersedes_id)
            if parent is None:
                issues.append(
                    VaultHealthIssue(
                        "error",
                        "missing_revision_parent",
                        "superseding memory references a missing parent",
                        item_id=item.id,
                    )
                )
            elif item.revision != parent.revision + 1:
                issues.append(
                    VaultHealthIssue(
                        "error",
                        "invalid_revision_sequence",
                        "superseding memory revision is not parent revision plus one",
                        item_id=item.id,
                    )
                )

    @staticmethod
    def _check_edges(
        items_by_id: dict[UUID, MemoryItem],
        edges: tuple[MemoryEdge, ...],
        issues: list[VaultHealthIssue],
    ) -> set[UUID]:
        linked: set[UUID] = set()
        for edge in edges:
            missing = [
                item_id
                for item_id in (edge.src_id, edge.dst_id)
                if item_id not in items_by_id
            ]
            if missing:
                issues.append(
                    VaultHealthIssue(
                        "error",
                        "broken_graph_endpoint",
                        "graph edge references a missing or out-of-workspace memory",
                        edge_id=edge.id,
                    )
                )
                continue
            linked.update((edge.src_id, edge.dst_id))
            if edge.provenance_item_id is not None and edge.provenance_item_id not in items_by_id:
                issues.append(
                    VaultHealthIssue(
                        "error",
                        "missing_graph_provenance",
                        "graph edge provenance references a missing or out-of-workspace memory",
                        edge_id=edge.id,
                    )
                )
        return linked

    @staticmethod
    def _check_observations(
        items_by_id: dict[UUID, MemoryItem],
        observations: tuple[Observation, ...],
        issues: list[VaultHealthIssue],
    ) -> set[UUID]:
        linked: set[UUID] = set()
        for observation in observations:
            for item_id in observation.evidence_ids:
                if item_id not in items_by_id:
                    issues.append(
                        VaultHealthIssue(
                            "error",
                            "missing_observation_evidence",
                            "observation evidence references a missing or out-of-workspace memory",
                            observation_id=observation.id,
                        )
                    )
                elif not observation.stale:
                    linked.add(item_id)
        return linked
