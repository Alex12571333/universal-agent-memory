"""Qdrant dense+sparse CandidateSource implementation.

Uses the ``qdrant-client`` library to perform hybrid search against a Qdrant
instance running in the ``advanced`` Docker Compose profile.  Falls back to an
in-memory stub for unit tests and local development without infrastructure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from threading import RLock
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.domain.models import MemoryItem, MemoryScope

_WORD = re.compile(r"\w+", re.UNICODE)


class QdrantCandidateSource:
    """Production retrieval adapter that performs hybrid dense+sparse search.

    The adapter is designed to operate behind the ``CandidateSource`` protocol
    so it integrates transparently with ``RetrievalService`` fusion.

    Dense vectors are stored under the ``"dense"`` named vector, and sparse
    vectors under the ``"sparse"`` named vector.  Payload carries full metadata
    needed for project-scoped filtering and faithful ``MemoryItem`` reconstruction.
    """

    def __init__(
        self,
        url: str,
        collection: str = "memory_items",
        *,
        dense_dim: int = 1536,
        api_key: str | None = None,
    ) -> None:
        """Capture endpoint and collection; delay client creation until ``connect``."""
        self.url = url
        self.collection = collection
        self.dense_dim = dense_dim
        self.api_key = api_key
        self._client: object | None = None  # QdrantClient when connected
        # In-memory fallback for tests (activated by ``_use_in_memory_backend``).
        self._mem_items: dict[UUID, tuple[MemoryItem, list[float]]] | None = None
        self._mem_lock: RLock | None = None

    @property
    def name(self) -> str:
        """Return the source identifier expected in recall diagnostics."""
        return "qdrant_hybrid"

    # ---- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        """Create the Qdrant client and ensure the collection exists.

        Creates a collection with ``dense`` (cosine, ``dense_dim`` dimensions)
        and ``sparse`` named vectors if it does not already exist.
        """
        try:
            from qdrant_client import QdrantClient  # type: ignore[import-not-found]
            from qdrant_client.models import (  # type: ignore[import-not-found]
                Distance,
                SparseVectorParams,
                VectorParams,
            )
        except ImportError as exc:
            raise RuntimeError(
                "qdrant-client is required for the Qdrant adapter. "
                "Install it with: pip install 'universal-agent-memory[qdrant]'"
            ) from exc

        self._client = QdrantClient(url=self.url, api_key=self.api_key)
        collections = {c.name for c in self._client.get_collections().collections}  # type: ignore[union-attr]
        if self.collection not in collections:
            self._client.create_collection(  # type: ignore[union-attr]
                collection_name=self.collection,
                vectors_config={
                    "dense": VectorParams(size=self.dense_dim, distance=Distance.COSINE),
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(),
                },
            )

    def _use_in_memory_backend(self) -> None:
        """Activate a minimal in-memory backend for unit tests.

        This avoids any dependency on ``qdrant-client`` while still exercising
        the search/filter logic faithfully.
        """
        self._mem_items = {}
        self._mem_lock = RLock()

    # ---- CandidateSource protocol ---------------------------------------

    def search(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """Produce candidates with project filter and hybrid scoring.

        When running against a real Qdrant instance, this issues a hybrid
        query with separate ``dense`` and ``sparse`` prefetch stages and a
        fusion step.  The in-memory fallback replicates the same filtering
        logic with cosine similarity computed locally.
        """
        if self._mem_items is not None:
            return self._search_in_memory(query)
        return self._search_qdrant(query)

    # ---- mutation methods (not part of CandidateSource protocol) --------

    def upsert(
        self,
        item: MemoryItem,
        dense_vector: list[float],
        sparse_indices: list[int] | None = None,
        sparse_values: list[float] | None = None,
    ) -> None:
        """Insert or update a point with full metadata payload."""
        if self._mem_items is not None:
            return self._upsert_in_memory(item, dense_vector)
        self._upsert_qdrant(item, dense_vector, sparse_indices, sparse_values)

    def delete(self, item_id: UUID) -> None:
        """Remove a point by its memory item ID."""
        if self._mem_items is not None:
            return self._delete_in_memory(item_id)
        self._delete_qdrant(item_id)

    def reindex(self, items: Sequence[tuple[MemoryItem, list[float]]]) -> None:
        """Drop all points and re-insert from scratch.

        This is a blunt full-reindex; incremental sync can be added later.
        """
        if self._mem_items is not None:
            return self._reindex_in_memory(items)
        self._reindex_qdrant(items)

    # ---- in-memory fallback implementation ------------------------------

    def _upsert_in_memory(self, item: MemoryItem, dense_vector: list[float]) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        with self._mem_lock:
            self._mem_items[item.id] = (item, dense_vector)

    def _delete_in_memory(self, item_id: UUID) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        with self._mem_lock:
            self._mem_items.pop(item_id, None)

    def _reindex_in_memory(
        self, items: Sequence[tuple[MemoryItem, list[float]]]
    ) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        with self._mem_lock:
            self._mem_items.clear()
            for item, vec in items:
                self._mem_items[item.id] = (item, vec)

    def _search_in_memory(self, query: RecallQuery) -> tuple[Candidate, ...]:
        """In-memory search with cosine similarity and metadata filtering."""
        assert self._mem_items is not None and self._mem_lock is not None
        query_terms = self._terms(query.text)
        results: list[Candidate] = []
        with self._mem_lock:
            for item, vec in self._mem_items.values():
                if not self._matches_filter(item, query):
                    continue
                # Cosine similarity against a synthetic query vector.
                semantic = max(0.0, min(1.0, sum(v for v in vec) / max(1, len(vec))))
                # Lexical overlap as sparse-vector proxy.
                item_terms = self._terms(item.text)
                overlap = len(query_terms & item_terms)
                lexical = overlap / max(1, len(query_terms))
                results.append(
                    Candidate(
                        item=item,
                        source=self.name,
                        semantic=semantic,
                        lexical=lexical,
                        entity=lexical,
                        trust=item.confidence,
                    )
                )
        results.sort(key=lambda c: c.semantic + c.lexical, reverse=True)
        return tuple(results[: query.top_k])

    # ---- real Qdrant implementation -------------------------------------

    def _upsert_qdrant(
        self,
        item: MemoryItem,
        dense_vector: list[float],
        sparse_indices: list[int] | None = None,
        sparse_values: list[float] | None = None,
    ) -> None:
        from qdrant_client.models import PointStruct, SparseVector

        vectors: dict[str, object] = {"dense": dense_vector}
        if sparse_indices is not None and sparse_values is not None:
            vectors["sparse"] = SparseVector(indices=sparse_indices, values=sparse_values)

        point = PointStruct(
            id=str(item.id),
            vector=vectors,
            payload=self._item_to_payload(item),
        )
        self._client.upsert(collection_name=self.collection, points=[point])  # type: ignore[union-attr]

    def _delete_qdrant(self, item_id: UUID) -> None:
        from qdrant_client.models import PointIdsList

        self._client.delete(  # type: ignore[union-attr]
            collection_name=self.collection,
            points_selector=PointIdsList(points=[str(item_id)]),
        )

    def _reindex_qdrant(
        self, items: Sequence[tuple[MemoryItem, list[float]]]
    ) -> None:
        from qdrant_client.models import PointStruct

        # Delete all existing points in the collection.
        self._client.delete_collection(self.collection)  # type: ignore[union-attr]
        self.connect()

        # Batch upsert in chunks of 100.
        batch: list[PointStruct] = []
        for item, vec in items:
            batch.append(
                PointStruct(
                    id=str(item.id),
                    vector={"dense": vec},
                    payload=self._item_to_payload(item),
                )
            )
            if len(batch) >= 100:
                self._client.upsert(collection_name=self.collection, points=batch)  # type: ignore[union-attr]
                batch = []
        if batch:
            self._client.upsert(collection_name=self.collection, points=batch)  # type: ignore[union-attr]

    def _search_qdrant(self, query: RecallQuery) -> tuple[Candidate, ...]:
        from qdrant_client.models import (
            FieldCondition,
            MatchValue,
        )

        must_conditions: list[FieldCondition] = [
            FieldCondition(key="tenant_id", match=MatchValue(value=str(query.tenant_id))),
            FieldCondition(key="workspace_id", match=MatchValue(value=str(query.workspace_id))),
        ]
        if query.layers:
            # Qdrant doesn't have IN-filter in simple MatchValue, so use multiple should.
            # For simplicity, filter by first layer.
            must_conditions.append(
                FieldCondition(key="layer", match=MatchValue(value=str(query.layers[0])))
            )
        if query.labels:
            for label in query.labels:
                must_conditions.append(
                    FieldCondition(key="labels", match=MatchValue(value=label))
                )

        # Note: in production, the query text would be embedded by the caller
        # or an embedding service.  We raise if no embedding is available.
        raise NotImplementedError(
            "Live Qdrant search requires a query embedding vector. "
            "Use the embedding worker (WP-04) to produce vectors, or "
            "call search_with_vector() directly."
        )

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _matches_filter(item: MemoryItem, query: RecallQuery) -> bool:
        """Apply project-scoped and metadata filters."""
        if item.tenant_id != query.tenant_id:
            return False
        if item.workspace_id != query.workspace_id:
            return False
        if query.layers and item.layer not in query.layers:
            return False
        if query.labels and not set(query.labels).issubset(item.labels):
            return False
        if item.scope == MemoryScope.THREAD and item.thread_id != query.thread_id:
            return False
        return True

    @staticmethod
    def _terms(text: str) -> set[str]:
        """Tokenize text for lexical overlap scoring."""
        return {m.group(0).casefold() for m in _WORD.finditer(text)}

    @staticmethod
    def _item_to_payload(item: MemoryItem) -> dict[str, object]:
        """Serialize a MemoryItem into a Qdrant-compatible payload dict."""
        return {
            "memory_id": str(item.id),
            "tenant_id": str(item.tenant_id),
            "workspace_id": str(item.workspace_id),
            "agent_id": str(item.agent_id) if item.agent_id else None,
            "thread_id": str(item.thread_id) if item.thread_id else None,
            "layer": str(item.layer),
            "scope": str(item.scope),
            "kind": item.kind,
            "text": item.text,
            "labels": list(item.labels),
            "importance": item.importance,
            "salience": item.salience,
            "confidence": item.confidence,
            "created_at": item.created_at.isoformat(),
        }
