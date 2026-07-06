"""Evidence-grounded consolidation used by asynchronous sleep cycles."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from memory_plane.domain.models import MemoryItem, MemoryLayer, Observation
from memory_plane.ports.repositories import MemoryLedger, ObservationRepository

_DATE_SEPARATORS = re.compile(r"[-/,]+")
_PUNCTUATION = re.compile(r"[^\w\s-]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class _BeliefSlot:
    """Heuristic extraction result for deterministic reflection grouping."""

    subject: str
    predicate: str
    value: str

    @property
    def key(self) -> str:
        """Group comparable facts about the same entity/relation."""
        return f"{self.subject}|{self.predicate}"


class ReflectionService:
    """Consolidate repeated semantic memories while retaining raw evidence."""

    def __init__(self, ledger: MemoryLedger, observations: ObservationRepository) -> None:
        """Bind reflection to canonical evidence and a derived observation store."""
        self._ledger = ledger
        self._observations = observations

    def reflect(self, tenant_id: UUID, workspace_id: UUID) -> tuple[Observation, ...]:
        """Create deterministic observations for repeated or conflicting beliefs."""
        items = self._ledger.list_for_workspace(
            tenant_id, workspace_id, layers=(MemoryLayer.SEMANTIC,)
        )
        groups: dict[str, list[tuple[_BeliefSlot, MemoryItem]]] = defaultdict(list)
        for item in items:
            slot = self._extract_slot(item.text)
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
                created.append(self._observations.save(observation))
                existing_keys.add(key)
        return tuple(created)

    def _extract_slot(self, text: str) -> _BeliefSlot:
        """Extract a conservative entity/relation/value slot from plain text."""
        normalized = self._normalize(text)
        owner = re.fullmatch(r"(?P<owner>.+?) owns (?P<thing>.+)", normalized)
        if owner:
            return _BeliefSlot(
                subject=self._normalize_entity(owner.group("thing")),
                predicate="owner",
                value=self._normalize_entity(owner.group("owner")),
            )

        release_date = re.fullmatch(
            r"(?P<subject>.+?) releases? on (?P<value>.+)", normalized
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

        return _BeliefSlot(
            subject=normalized,
            predicate="statement",
            value="true",
        )

    def _normalize_entity(self, text: str) -> str:
        """Normalize entity keys while preserving enough meaning for audit."""
        return self._normalize(text.removeprefix("the "))

    def _normalize_value(self, text: str) -> str:
        """Normalize comparable slot values, including common date separators."""
        return _DATE_SEPARATORS.sub(" ", self._normalize(text))

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text only enough for safe exact-belief grouping."""
        text = _PUNCTUATION.sub(" ", text.casefold().strip())
        return " ".join(text.split())

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
