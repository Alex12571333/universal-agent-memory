"""Hybrid candidate fan-out, tenant-safe fusion and ranking."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery, RecallResult
from memory_plane.ports.repositories import CandidateSource

DEFAULT_WEIGHTS = {
    "semantic": 0.35,
    "lexical": 0.20,
    "entity": 0.15,
    "recency": 0.10,
    "importance": 0.10,
    "trust": 0.10,
}


class RetrievalService:
    """Combine independent dense, sparse, graph and SQL sources."""

    def __init__(
        self,
        sources: tuple[CandidateSource, ...],
        weights: dict[str, float] | None = None,
    ) -> None:
        """Configure candidate sources and an explicit, inspectable score formula."""
        if not sources:
            raise ValueError("at least one candidate source is required")
        self._sources = sources
        self._weights = weights or DEFAULT_WEIGHTS
        if abs(sum(self._weights.values()) - 1.0) > 1e-9:
            raise ValueError("retrieval weights must sum to 1.0")

    def recall(self, query: RecallQuery) -> RecallResult:
        """Fan out to sources, merge duplicate IDs, score, filter and rank."""
        grouped: dict[UUID, list[Candidate]] = defaultdict(list)
        used: list[str] = []
        for source in self._sources:
            candidates = source.search(query)
            used.append(source.name)
            for candidate in candidates:
                if candidate.item.tenant_id != query.tenant_id:
                    continue
                if candidate.item.workspace_id != query.workspace_id:
                    continue
                if query.valid_at and not candidate.item.is_valid_at(query.valid_at):
                    continue
                grouped[candidate.item.id].append(candidate)

        ranked = [self._fuse(rows) for rows in grouped.values()]
        ranked = [row for row in ranked if row.final_score >= query.minimum_score]
        ranked.sort(key=lambda row: (row.final_score, row.item.created_at), reverse=True)
        return RecallResult(candidates=tuple(ranked[: query.top_k]), sources_used=tuple(used))

    def _fuse(self, candidates: list[Candidate]) -> Candidate:
        """Fuse signals for one item by retaining each source's strongest evidence."""
        item = candidates[0].item
        signals = {
            "semantic": max(row.semantic for row in candidates),
            "lexical": max(row.lexical for row in candidates),
            "entity": max(row.entity for row in candidates),
            "recency": max(max(row.recency, self._recency(item.created_at)) for row in candidates),
            "trust": max(max(row.trust, item.confidence) for row in candidates),
        }
        score = (
            self._weights["semantic"] * signals["semantic"]
            + self._weights["lexical"] * signals["lexical"]
            + self._weights["entity"] * signals["entity"]
            + self._weights["recency"] * signals["recency"]
            + self._weights["importance"] * item.importance
            + self._weights["trust"] * signals["trust"]
        )
        sources = "+".join(sorted({row.source for row in candidates}))
        return replace(
            candidates[0],
            source=sources,
            semantic=signals["semantic"],
            lexical=signals["lexical"],
            entity=signals["entity"],
            recency=signals["recency"],
            trust=signals["trust"],
            final_score=min(1.0, score),
        )

    @staticmethod
    def _recency(created_at: datetime) -> float:
        """Map age to a smooth 0..1 score with a 30-day half-life."""
        age_days = max(0.0, (datetime.now(UTC) - created_at).total_seconds() / 86_400)
        return float(0.5 ** (age_days / 30.0))
