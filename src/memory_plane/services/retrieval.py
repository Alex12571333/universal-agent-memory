"""Hybrid candidate fan-out, tenant-safe fusion and ranking."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock
from typing import Any
from uuid import UUID

from memory_plane.contracts.dto import (
    Candidate,
    IndexFreshness,
    RecallQuery,
    RecallResult,
    RetrievalTraceStep,
)
from memory_plane.domain.models import MemoryScope, MemoryStatus
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
        required_sources: frozenset[str] | None = None,
        staleness_check: Callable[[RecallQuery], bool | IndexFreshness] | None = None,
    ) -> None:
        """Configure candidate sources and an explicit, inspectable score formula."""
        if not sources:
            raise ValueError("at least one candidate source is required")
        self._sources = sources
        self._weights = weights or DEFAULT_WEIGHTS
        self._required_sources = required_sources or frozenset({sources[0].name})
        self._staleness_check = staleness_check
        self._health_lock = RLock()
        self._source_health: dict[str, dict[str, Any]] = {
            source.name: {"status": "unknown", "failures": 0, "error_type": None}
            for source in sources
        }
        if abs(sum(self._weights.values()) - 1.0) > 1e-9:
            raise ValueError("retrieval weights must sum to 1.0")

    def recall(self, query: RecallQuery) -> RecallResult:
        """Fan out to sources, merge duplicate IDs, score, filter and rank."""
        grouped: dict[UUID, list[Candidate]] = defaultdict(list)
        used: list[str] = []
        traversal: list[RetrievalTraceStep] = []
        for source in self._sources:
            try:
                candidates = source.search(query)
            except Exception as exc:
                self.record_failure(source.name, exc)
                if len(traversal) < 63:
                    traversal.append(
                        RetrievalTraceStep(
                            sequence=len(traversal) + 1,
                            stage="source",
                            name=_trace_name(source.name),
                            status="degraded",
                            error_type=_trace_error_type(exc),
                        )
                    )
                if source.name in self._required_sources:
                    raise
                continue
            self.record_success(source.name)
            used.append(source.name)
            accepted_count = 0
            for candidate in candidates:
                if candidate.item.tenant_id != query.tenant_id:
                    continue
                if candidate.item.workspace_id != query.workspace_id:
                    continue
                if query.valid_at and not candidate.item.is_valid_at(query.valid_at):
                    continue
                if candidate.item.status in (MemoryStatus.REJECTED, MemoryStatus.ARCHIVED):
                    continue
                if (
                    candidate.item.scope == MemoryScope.PRIVATE
                    and candidate.item.agent_id != query.agent_id
                ):
                    continue
                if (
                    candidate.item.scope == MemoryScope.THREAD
                    and candidate.item.thread_id != query.thread_id
                ):
                    continue
                grouped[candidate.item.id].append(candidate)
                accepted_count += 1
            if len(traversal) < 63:
                traversal.append(
                    RetrievalTraceStep(
                        sequence=len(traversal) + 1,
                        stage="source",
                        name=_trace_name(source.name),
                        status="succeeded",
                        candidate_count=len(candidates),
                        accepted_count=accepted_count,
                    )
                )

        ranked = [self._fuse(rows) for rows in grouped.values()]
        ranked = [row for row in ranked if row.final_score >= query.minimum_score]
        ranked.sort(key=lambda row: (row.final_score, row.item.created_at), reverse=True)
        selected = tuple(ranked[: query.top_k])
        traversal.append(
            RetrievalTraceStep(
                sequence=len(traversal) + 1,
                stage="fusion",
                name="weighted-fusion",
                status="succeeded",
                candidate_count=len(grouped),
                accepted_count=len(ranked),
                selected_count=len(selected),
            )
        )
        freshness = self._index_freshness(query)
        return RecallResult(
            candidates=selected,
            sources_used=tuple(used),
            index_stale=freshness.stale if freshness is not None else False,
            index_freshness=freshness,
            traversal=tuple(traversal),
        )

    def _index_freshness(self, query: RecallQuery) -> IndexFreshness | None:
        """Return durable vector-delivery detail, failing closed when unknown."""
        if self._staleness_check is None:
            return None
        try:
            value = self._staleness_check(query)
            if isinstance(value, IndexFreshness):
                return value
            return IndexFreshness(stale_memory_count=1 if bool(value) else 0)
        except Exception:  # noqa: BLE001 - unknown freshness must not be reported as fresh.
            return IndexFreshness(stale_memory_count=1, missing_delivery_memory_count=1)

    def record_success(self, source_name: str) -> None:
        """Mark a candidate source healthy after a completed operation."""
        with self._health_lock:
            state = self._source_health.setdefault(
                source_name,
                {"status": "unknown", "failures": 0, "error_type": None},
            )
            state["status"] = "healthy"
            state["error_type"] = None

    def record_failure(self, source_name: str, error: Exception) -> None:
        """Record failure type without retaining endpoint or credential text."""
        with self._health_lock:
            state = self._source_health.setdefault(
                source_name,
                {"status": "unknown", "failures": 0, "error_type": None},
            )
            state["status"] = (
                "failed" if source_name in self._required_sources else "degraded"
            )
            state["failures"] = int(state["failures"]) + 1
            state["error_type"] = type(error).__name__

    def source_health(self) -> dict[str, dict[str, Any]]:
        """Return a copy of dependency status safe for readiness responses."""
        with self._health_lock:
            return {name: dict(state) for name, state in self._source_health.items()}

    def collect_metrics(self) -> dict[str, int]:
        """Return aggregate source-failure metrics for the API exporter."""
        with self._health_lock:
            return {
                "retrieval_source_failures_total": sum(
                    int(state["failures"]) for state in self._source_health.values()
                ),
                "retrieval_degraded_sources": sum(
                    state["status"] in {"degraded", "failed"}
                    for state in self._source_health.values()
                ),
            }

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
        score *= self._status_multiplier(item.status)
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
    def _status_multiplier(status: MemoryStatus) -> float:
        """Demote uncertain states while boosting pinned core memory."""
        if status == MemoryStatus.PINNED:
            return 1.15
        if status in (MemoryStatus.DISPUTED, MemoryStatus.HYPOTHESIS):
            return 0.55
        if status in (MemoryStatus.STALE, MemoryStatus.DEPRECATED):
            return 0.35
        return 1.0

    @staticmethod
    def _recency(created_at: datetime) -> float:
        """Map age to a smooth 0..1 score with a 30-day half-life."""
        age_days = max(0.0, (datetime.now(UTC) - created_at).total_seconds() / 86_400)
        return float(0.5 ** (age_days / 30.0))


def _trace_name(value: object) -> str:
    """Bound adapter-controlled names before they enter durable audit metadata."""
    normalized = str(value).strip()[:64]
    return normalized or "unknown-source"


def _trace_error_type(error: Exception) -> str:
    """Record only a bounded class name, never an exception message."""
    normalized = type(error).__name__.strip()[:128]
    return normalized or "DependencyError"
