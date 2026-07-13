"""Evidence-grounded consolidation used by asynchronous sleep cycles."""

from __future__ import annotations

from collections import defaultdict
from uuid import NAMESPACE_URL, UUID, uuid5

from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.models import MemoryItem, MemoryLayer, Observation
from memory_plane.ports.repositories import MemoryLedger, ObservationRepository
from memory_plane.services.belief_slots import BeliefSlot, extract_belief_slot


class ReflectionService:
    """Consolidate repeated semantic memories while retaining raw evidence."""

    def __init__(self, ledger: MemoryLedger, observations: ObservationRepository) -> None:
        """Bind reflection to canonical evidence and a derived observation store."""
        self._ledger = ledger
        self._observations = observations

    def reflect(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        actor: str = "maintenance",
        actor_type: str = "system",
    ) -> tuple[Observation, ...]:
        """Create deterministic observations for repeated or conflicting beliefs."""
        items = self._ledger.list_for_workspace(
            tenant_id, workspace_id, layers=(MemoryLayer.SEMANTIC,)
        )
        groups: dict[str, list[tuple[BeliefSlot, MemoryItem]]] = defaultdict(list)
        for item in items:
            slot = extract_belief_slot(item.text)
            groups[slot.key].append((slot, item))

        existing = self._observations.list_for_workspace(tenant_id, workspace_id)
        existing_keys = {
            self._observation_key(row.summary, row.evidence_ids, row.stale)
            for row in existing
        }

        created: list[Observation] = []
        for facts in groups.values():
            by_value: dict[str, list[MemoryItem]] = defaultdict(list)
            for slot, item in facts:
                by_value[slot.value].append(item)
            if len(facts) < 2:
                continue

            latest = max((item for _, item in facts), key=lambda row: row.created_at)
            for _value, rows in sorted(by_value.items()):
                if len(rows) < 2 and len(by_value) == 1:
                    continue
                newest_for_value = max(rows, key=lambda row: row.created_at)
                stale = newest_for_value.id != latest.id
                evidence_ids = tuple(row.id for row in rows)
                summary = newest_for_value.text
                key = self._observation_key(summary, evidence_ids, stale)
                if key in existing_keys:
                    continue
                observation = Observation(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"uam:reflection:{tenant_id}:{workspace_id}:{key}",
                    ),
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    summary=summary,
                    evidence_ids=evidence_ids,
                    confidence=self._confidence(rows, conflict=len(by_value) > 1),
                    stale=stale,
                )
                created.append(
                    self._observations.save(
                        observation,
                        audit_event=AuditEvent(
                            tenant_id=tenant_id,
                            workspace_id=workspace_id,
                            action="reflection.observation.create",
                            actor=actor,
                            actor_type=actor_type,
                            resource_type="observation",
                            resource_id=str(observation.id),
                            metadata={
                                "evidence_count": len(observation.evidence_ids),
                                "stale": observation.stale,
                            },
                        ),
                    )
                )
                existing_keys.add(key)
        return tuple(created)

    @staticmethod
    def _confidence(rows: list[MemoryItem], *, conflict: bool) -> float:
        """Boost repeated evidence, but penalize slots with conflicting values."""
        base = sum(row.confidence for row in rows) / len(rows)
        boost = 0.1 if len(rows) > 1 else 0.0
        penalty = 0.15 if conflict else 0.0
        return min(1.0, max(0.0, base + boost - penalty))

    @staticmethod
    def _observation_key(
        summary: str, evidence_ids: tuple[UUID, ...], stale: bool
    ) -> str:
        """Build a stable dedupe key independent of generated observation IDs."""
        evidence = ",".join(str(item_id) for item_id in sorted(evidence_ids))
        return f"{summary}|{evidence}|stale={stale}"
