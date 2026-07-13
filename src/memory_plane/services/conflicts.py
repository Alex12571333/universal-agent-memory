"""Conflict resolver and review inbox."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from uuid import NAMESPACE_URL, UUID, uuid5

from memory_plane.contracts.events import IntegrationEvent
from memory_plane.domain.audit import AuditEvent
from memory_plane.domain.conflict import (
    ConflictCandidate,
    ConflictCase,
    ConflictReviewDecision,
    ConflictReviewStatus,
)
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryStatus,
    Provenance,
)
from memory_plane.ports.repositories import ConflictReviewRepository, MemoryLedger
from memory_plane.services.belief_slots import BeliefSlot, extract_belief_slot


class ConflictService:
    """Build an inspectable conflict inbox from append-only memory evidence."""

    def __init__(
        self,
        ledger: MemoryLedger,
        reviews: ConflictReviewRepository,
    ) -> None:
        """Bind to source-of-truth memories and persisted review decisions."""
        self._ledger = ledger
        self._reviews = reviews

    def list_cases(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        *,
        include_resolved: bool = False,
    ) -> tuple[ConflictCase, ...]:
        """Return deterministic conflict cases for semantic memories."""
        items = self._ledger.list_for_workspace(
            tenant_id,
            workspace_id,
            layers=(MemoryLayer.SEMANTIC,),
        )
        groups: dict[str, list[tuple[BeliefSlot, MemoryItem]]] = defaultdict(list)
        for item in items:
            slot = extract_belief_slot(item.text)
            groups[slot.key].append((slot, item))

        reviews = {
            decision.case_id: decision
            for decision in self._reviews.list_for_workspace(tenant_id, workspace_id)
        }

        cases: list[ConflictCase] = []
        for facts in groups.values():
            by_value: dict[str, list[MemoryItem]] = defaultdict(list)
            for slot, item in facts:
                by_value[slot.value].append(item)
            if len(by_value) < 2:
                continue

            latest = max((item for _, item in facts), key=lambda row: row.created_at)
            first_slot = facts[0][0]
            case_id = self._case_id(tenant_id, workspace_id, first_slot)
            review = reviews.get(case_id)
            candidates: list[ConflictCandidate] = []
            for value, rows in sorted(by_value.items()):
                newest_for_value = max(rows, key=lambda row: row.created_at)
                if review is not None and review.applied_memory_id is not None:
                    status = "active" if value == review.winner_value else "stale"
                else:
                    status = "active" if newest_for_value.id == latest.id else "stale"
                confidence = self._candidate_confidence(rows, is_active=status == "active")
                candidates.append(
                    ConflictCandidate(
                        value=value,
                        status=status,
                        evidence_ids=tuple(row.id for row in rows),
                        confidence=confidence,
                        latest_created_at=newest_for_value.created_at,
                    )
                )
            if (
                not include_resolved
                and review is not None
                and review.status != ConflictReviewStatus.UNRESOLVED
            ):
                continue
            suggested = max(
                candidates,
                key=lambda row: (
                    row.status == "active",
                    row.latest_created_at,
                    row.confidence,
                ),
            )
            cases.append(
                ConflictCase(
                    id=case_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    subject=first_slot.subject,
                    predicate=first_slot.predicate,
                    candidates=tuple(candidates),
                    suggested_winner_value=suggested.value,
                    suggested_reason=(
                        "newest active value with strongest evidence; "
                        "raw memories remain append-only"
                    ),
                    review=review,
                )
            )
        return tuple(sorted(cases, key=lambda row: (row.subject, row.predicate, row.id)))

    def decide(
        self,
        tenant_id: UUID,
        workspace_id: UUID,
        case_id: UUID,
        *,
        status: ConflictReviewStatus,
        winner_value: str | None,
        reason: str,
        audit_event: AuditEvent | None = None,
    ) -> ConflictReviewDecision:
        """Persist review and atomically make its winner canonical when required."""
        if status in (ConflictReviewStatus.ACCEPTED, ConflictReviewStatus.OVERRIDDEN):
            if not winner_value or not winner_value.strip():
                raise ValueError("winner_value is required for accepted/overridden decisions")
        cases = self.list_cases(tenant_id, workspace_id, include_resolved=True)
        case = next((row for row in cases if row.id == case_id), None)
        if case is None:
            raise ValueError("conflict case not found")
        if case.review is not None and case.review.applied_memory_id is not None:
            if case.review.status == status and case.review.winner_value == winner_value:
                return case.review
            raise ValueError("conflict resolution is already applied and immutable")
        decision = ConflictReviewDecision(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            case_id=case_id,
            status=status,
            winner_value=winner_value,
            reason=reason,
        )
        if status not in (ConflictReviewStatus.ACCEPTED, ConflictReviewStatus.OVERRIDDEN):
            return self._reviews.save(decision, audit_event=audit_event)
        assert winner_value is not None
        return self._apply_winner(case, decision, winner_value.strip(), audit_event)

    def _apply_winner(
        self,
        case: ConflictCase,
        decision: ConflictReviewDecision,
        winner_value: str,
        audit_event: AuditEvent | None = None,
    ) -> ConflictReviewDecision:
        by_value = {candidate.value: candidate for candidate in case.candidates}
        winner = by_value.get(winner_value)
        if winner is None:
            raise ValueError("winner_value must match one of the conflict candidates")
        evidence_ids = tuple(
            evidence_id
            for candidate in case.candidates
            for evidence_id in candidate.evidence_ids
        )
        items = {
            item_id: item
            for item_id in evidence_ids
            if (item := self._ledger.get(case.tenant_id, item_id)) is not None
        }
        if len(items) != len(set(evidence_ids)):
            raise ValueError("conflict evidence is incomplete")
        recallable = self._ledger.filter_recallable_heads(
            case.tenant_id,
            tuple(items),
        )
        winner_items = [items[item_id] for item_id in winner.evidence_ids]
        winner_source = max(winner_items, key=lambda item: (item.created_at, item.id))
        winner_heads = [item for item in winner_items if item.id in recallable]
        loser_heads = [
            items[item_id]
            for candidate in case.candidates
            if candidate.value != winner_value
            for item_id in candidate.evidence_ids
            if item_id in recallable
        ]

        writes: list[tuple[MemoryItem, IntegrationEvent, int]] = []
        if winner_heads:
            applied = max(winner_heads, key=lambda item: (item.created_at, item.id))
        else:
            if not loser_heads:
                raise ValueError("conflict has no recallable head to supersede")
            parent = max(loser_heads, key=lambda item: (item.created_at, item.id))
            loser_heads.remove(parent)
            applied = parent.supersede(
                winner_source.text,
                confidence=winner_source.confidence,
                status=MemoryStatus.ACTIVE,
            )
            applied = replace(
                applied,
                provenance=Provenance(
                    source_kind="conflict-review",
                    origin_uri=f"conflict://{case.id}",
                    object_key=str(winner_source.id),
                    quote=winner_source.text,
                    extraction_version="operator-resolution-v1",
                ),
                metadata={
                    **applied.metadata,
                    "conflict_case_id": str(case.id),
                    "winner_evidence_id": str(winner_source.id),
                },
            )
            writes.append((applied, self._resolution_event(applied), parent.revision))

        for loser in loser_heads:
            tombstone = loser.supersede(loser.text, status=MemoryStatus.ARCHIVED)
            tombstone = replace(
                tombstone,
                metadata={
                    **tombstone.metadata,
                    "conflict_case_id": str(case.id),
                    "archived_by_conflict_resolution": True,
                },
            )
            writes.append((tombstone, self._resolution_event(tombstone), loser.revision))
        applied_decision = replace(decision, applied_memory_id=applied.id)
        return self._reviews.apply_resolution(
            applied_decision, tuple(writes), audit_event=audit_event
        )

    @staticmethod
    def _resolution_event(item: MemoryItem) -> IntegrationEvent:
        return IntegrationEvent(
            name="memory.retained.v1",
            tenant_id=item.tenant_id,
            workspace_id=item.workspace_id,
            correlation_id=item.id,
            payload={
                "memory_id": str(item.id),
                "supersedes_id": str(item.supersedes_id),
                "revision": item.revision,
                "layer": item.layer.value,
                "jobs": ["embed", "reflect"],
                "reason": "conflict-resolution",
            },
        )

    def _case_id(self, tenant_id: UUID, workspace_id: UUID, slot: BeliefSlot) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"uam:conflict:{tenant_id}:{workspace_id}:{slot.key}",
        )

    @staticmethod
    def _candidate_confidence(rows: list[MemoryItem], *, is_active: bool) -> float:
        base = sum(row.confidence for row in rows) / len(rows)
        boost = min(0.2, 0.05 * (len(rows) - 1))
        active_boost = 0.05 if is_active else 0.0
        return min(1.0, max(0.0, base + boost + active_boost))
