"""Service for asynchronous memory embedding and indexing."""

from __future__ import annotations

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

    def process_memory_retained(self, tenant_id: UUID, memory_id: UUID) -> None:
        """Generate embedding for the retained memory and upsert it into the vector store."""
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
        # Store embedding in Qdrant with model metadata in payload
        self._qdrant.upsert(item, dense_vector=vector, model_name=self._client.model_name)

    def reindex_all(self, tenant_id: UUID, workspace_id: UUID) -> int:
        """Re-generate all embeddings using the current model and perform a full reindex."""
        items = self._ledger.list_for_workspace(tenant_id, workspace_id)
        if not items:
            return 0

        superseded_ids = {item.supersedes_id for item in items if item.supersedes_id is not None}
        pairs = []
        for item in items:
            if item.id in superseded_ids or item.status.value in {"archived", "rejected"}:
                continue
            vector = self._embed_document(item.text)
            self._validate_dimension(vector)
            pairs.append((item, vector))

        self._qdrant.reindex(pairs, model_name=self._client.model_name)
        return len(pairs)

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
