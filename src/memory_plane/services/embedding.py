"""Service for asynchronous memory embedding and indexing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from memory_plane.adapters.qdrant import QdrantCandidateSource
    from memory_plane.ports.embeddings import EmbeddingClient
    from memory_plane.ports.repositories import MemoryLedger


class EmbeddingService:
    """Coordinate generation and indexing of dense memory representations."""

    def __init__(
        self,
        ledger: MemoryLedger,
        qdrant: QdrantCandidateSource,
        client: EmbeddingClient,
    ) -> None:
        """Initialize service dependencies."""
        self._ledger = ledger
        self._qdrant = qdrant
        self._client = client
        self._embed_total = 0
        self._embed_failures_total = 0
        self._embed_duration_seconds_sum = 0.0
        self._embed_last_duration_seconds = 0.0
        self._reindex_total = 0
        self._reindex_failures_total = 0
        self._reindex_last_duration_seconds = 0.0

    def process_memory_retained(self, tenant_id: UUID, memory_id: UUID) -> None:
        """Generate embedding for the retained memory and upsert it into the vector store."""
        started = time.perf_counter()
        try:
            item = self._ledger.get(tenant_id, memory_id)
            if item is None:
                raise ValueError(f"Memory item {memory_id} not found for tenant {tenant_id}")
            if item.status.value in {"archived", "rejected"}:
                self._qdrant.delete(item.id)
                if item.supersedes_id is not None:
                    self._qdrant.delete(item.supersedes_id)
                return

            vector = self._embed_document(item.text)
            self._validate_dimension(vector)
            self._qdrant.upsert(item, dense_vector=vector, model_name=self._client.model_name)
        except Exception:
            self._embed_failures_total += 1
            raise
        finally:
            self._record_embed_duration(started)

    def reindex_all(self, tenant_id: UUID, workspace_id: UUID) -> int:
        """Re-generate all embeddings using the current model and perform a full reindex."""
        started = time.perf_counter()
        try:
            items = self._ledger.list_for_workspace(tenant_id, workspace_id)
            if not items:
                self._reindex_total += 1
                return 0

            superseded_ids = {
                item.supersedes_id
                for item in items
                if item.supersedes_id is not None
            }
            pairs = []
            for item in items:
                if item.id in superseded_ids or item.status.value in {"archived", "rejected"}:
                    continue
                vector = self._embed_document(item.text)
                self._validate_dimension(vector)
                pairs.append((item, vector))

            self._qdrant.reindex(pairs, model_name=self._client.model_name)
            self._reindex_total += 1
            return len(pairs)
        except Exception:
            self._reindex_failures_total += 1
            raise
        finally:
            self._reindex_last_duration_seconds = time.perf_counter() - started

    def collect_metrics(self) -> dict[str, float | int]:
        """Return process-local embedding health metrics."""
        return {
            "embedding_operations_total": self._embed_total,
            "embedding_failures_total": self._embed_failures_total,
            "embedding_duration_seconds_sum": round(self._embed_duration_seconds_sum, 6),
            "embedding_last_duration_seconds": round(self._embed_last_duration_seconds, 6),
            "embedding_reindex_total": self._reindex_total,
            "embedding_reindex_failures_total": self._reindex_failures_total,
            "embedding_reindex_last_duration_seconds": round(
                self._reindex_last_duration_seconds,
                6,
            ),
        }

    def _record_embed_duration(self, started: float) -> None:
        """Update embedding operation counters after one processing attempt."""
        duration = time.perf_counter() - started
        self._embed_total += 1
        self._embed_last_duration_seconds = duration
        self._embed_duration_seconds_sum += duration

    def _validate_dimension(self, vector: list[float]) -> None:
        """Reject provider output that cannot fit the configured vector index."""
        actual = len(vector)
        expected = self._client.dimension
        if actual != expected:
            raise ValueError(
                f"embedding dimension mismatch for {self._client.model_name}: "
                f"expected {expected}, got {actual}"
            )

    def _embed_document(self, text: str) -> list[float]:
        """Use document-specific embeddings when the provider exposes them."""
        embed_document = getattr(self._client, "embed_document", None)
        if callable(embed_document):
            return embed_document(text)
        return self._client.embed(text)
