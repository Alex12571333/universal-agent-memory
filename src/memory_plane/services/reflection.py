"""Evidence-grounded consolidation used by asynchronous sleep cycles."""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from memory_plane.domain.models import MemoryItem, MemoryLayer, Observation
from memory_plane.ports.repositories import MemoryLedger, ObservationRepository


class ReflectionService:
    """Consolidate repeated semantic memories while retaining raw evidence."""

    def __init__(self, ledger: MemoryLedger, observations: ObservationRepository) -> None:
        """Bind reflection to canonical evidence and a derived observation store."""
        self._ledger = ledger
        self._observations = observations

    def reflect(self, tenant_id: UUID, workspace_id: UUID) -> tuple[Observation, ...]:
        """Create observations for repeated normalized semantic statements.

        This conservative baseline consolidates only exact normalized statements.
        An LLM/embedding implementation can replace grouping behind the same
        service contract without changing raw memory or API callers.
        """
        items = self._ledger.list_for_workspace(
            tenant_id, workspace_id, layers=(MemoryLayer.SEMANTIC,)
        )
        groups: dict[str, list[MemoryItem]] = defaultdict(list)
        for item in items:
            groups[self._normalize(item.text)].append(item)

        created: list[Observation] = []
        for rows in groups.values():
            if len(rows) < 2:
                continue
            observation = Observation(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                summary=rows[-1].text,
                evidence_ids=tuple(row.id for row in rows),
                confidence=min(1.0, sum(row.confidence for row in rows) / len(rows) + 0.1),
            )
            created.append(self._observations.save(observation))
        return tuple(created)

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text only enough for safe exact-belief grouping."""
        return " ".join(text.casefold().strip().rstrip(".!?").split())
