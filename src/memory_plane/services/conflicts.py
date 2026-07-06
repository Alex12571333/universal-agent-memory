"""Conflict resolver and review inbox."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from memory_plane.domain.conflict import (
    ConflictCandidate,
    ConflictCase,
    ConflictReviewDecision,
    ConflictReviewStatus,
)
from memory_plane.domain.models import MemoryItem, MemoryLayer
from memory_plane.ports.repositories import ConflictReviewRepository, MemoryLedger

_DATE_SEPARATORS = re.compile(r"[-/,]+")
_PUNCTUATION = re.compile(r"[^\w\s-]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class _BeliefSlot:
    """Comparable memory slot."""

    subject: str
    predicate: str
    value: str

    @property
    def key(self) -> str:
        """Group comparable facts about the same entity/relation."""
        return f"{self.subject}|{self.predicate}"


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
        groups: dict[str, list[tuple[_BeliefSlot, MemoryItem]]] = defaultdict(list)
        for item in items:
            slot = self._extract_slot(item.text)
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
            candidates: list[ConflictCandidate] = []
            for value, rows in sorted(by_value.items()):
                newest_for_value = max(rows, key=lambda row: row.created_at)
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

            first_slot = facts[0][0]
            case_id = self._case_id(tenant_id, workspace_id, first_slot)
            review = reviews.get(case_id)
            if (
                not include_resolved
                and review is not None
                and review.status != ConflictReviewStatus.UNRESOLVED
            ):
                continue
            suggested = max(candidates, key=lambda row: (row.status == "active", row.confidence))
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
    ) -> ConflictReviewDecision:
        """Persist a human review decision for one case."""
        if status in (ConflictReviewStatus.ACCEPTED, ConflictReviewStatus.OVERRIDDEN):
            if not winner_value or not winner_value.strip():
                raise ValueError("winner_value is required for accepted/overridden decisions")
        decision = ConflictReviewDecision(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            case_id=case_id,
            status=status,
            winner_value=winner_value,
            reason=reason,
        )
        return self._reviews.save(decision)

    def _extract_slot(self, text: str) -> _BeliefSlot:
        normalized = self._normalize(text)
        owner = re.fullmatch(r"(?P<owner>.+?) owns (?P<thing>.+)", normalized)
        if owner:
            return _BeliefSlot(
                subject=self._normalize_entity(owner.group("thing")),
                predicate="owner",
                value=self._normalize_entity(owner.group("owner")),
            )

        release_date = re.fullmatch(
            r"(?P<subject>.+?) releases? on (?P<value>.+)",
            normalized,
        )
        if release_date:
            return _BeliefSlot(
                subject=self._normalize_entity(release_date.group("subject")),
                predicate="release_date",
                value=self._normalize_value(release_date.group("value")),
            )

        state = re.fullmatch(
            r"(?P<subject>.+?) (?:is|are|was|were|will be) (?P<value>.+)",
            normalized,
        )
        if state:
            return _BeliefSlot(
                subject=self._normalize_entity(state.group("subject")),
                predicate="state",
                value=self._normalize_value(state.group("value")),
            )

        return _BeliefSlot(subject=normalized, predicate="statement", value="true")

    def _case_id(self, tenant_id: UUID, workspace_id: UUID, slot: _BeliefSlot) -> UUID:
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

    def _normalize_entity(self, text: str) -> str:
        return self._normalize(text.removeprefix("the "))

    def _normalize_value(self, text: str) -> str:
        return _DATE_SEPARATORS.sub(" ", self._normalize(text))

    @staticmethod
    def _normalize(text: str) -> str:
        text = _PUNCTUATION.sub(" ", text.casefold().strip())
        return " ".join(text.split())
