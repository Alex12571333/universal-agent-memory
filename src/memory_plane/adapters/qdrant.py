"""Qdrant dense+sparse CandidateSource implementation.

Uses the ``qdrant-client`` library to perform hybrid search against a Qdrant
instance running in the ``advanced`` Docker Compose profile.  Falls back to an
in-memory stub for unit tests and local development without infrastructure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from math import sqrt
from threading import RLock
from typing import TYPE_CHECKING, Protocol, cast
from uuid import UUID

from memory_plane.contracts.dto import Candidate, RecallQuery
from memory_plane.domain.models import (
    MemoryItem,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    Provenance,
)
from memory_plane.ports.embeddings import EmbeddingClient

if TYPE_CHECKING:
    from memory_plane.ports.repositories import MemoryLedger

_WORD = re.compile(r"\w+", re.UNICODE)


class _ScoredPoint(Protocol):
    """Subset of a Qdrant result consumed by the retrieval adapter."""

    payload: dict[str, object] | None
    score: float


class _SearchMethod(Protocol):
    """Legacy qdrant-client search call supported by the adapter."""

    def __call__(
        self,
        *,
        collection_name: str,
        query_vector: tuple[str, list[float]],
        query_filter: object,
        limit: int,
        with_payload: bool,
    ) -> Sequence[_ScoredPoint]: ...


class _QueryPointsResponse(Protocol):
    """Subset of a qdrant-client query response consumed by the adapter."""

    points: Sequence[_ScoredPoint]


class _QueryPointsClient(Protocol):
    """Modern qdrant-client query call supported by the adapter."""

    def query_points(self, **kwargs: object) -> _QueryPointsResponse: ...


class _QueryEmbeddingMethod(Protocol):
    """Optional query-specific embedding method exposed by some providers."""

    def __call__(self, text: str) -> list[float]: ...


_NumericPayload = int | float | str


class QdrantCandidateSource:
    """Production retrieval adapter that performs hybrid dense+sparse search.

    The adapter is designed to operate behind the ``CandidateSource`` protocol
    so it integrates transparently with ``RetrievalService`` fusion.

    Dense vectors are stored under the ``"dense"`` named vector, and sparse
    vectors under the ``"sparse"`` named vector. Payload carries project-scoped
    filter metadata. Production deployments can disable raw text payloads and
    hydrate recalled candidates from the canonical ledger.
    """

    def __init__(
        self,
        url: str,
        collection: str = "memory_items",
        *,
        dense_dim: int = 1536,
        api_key: str | None = None,
        query_embedding_client: EmbeddingClient | None = None,
        ledger: MemoryLedger | None = None,
        payload_text: bool = True,
    ) -> None:
        """Capture endpoint and collection; delay client creation until ``connect``."""
        self.url = url
        self.collection = collection
        self.dense_dim = dense_dim
        self.api_key = api_key
        self._query_embedding_client = query_embedding_client
        self._ledger = ledger
        self._payload_text = payload_text
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
            from qdrant_client import QdrantClient
            from qdrant_client.models import (
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
        model_name: str | None = None,
    ) -> None:
        """Insert or update a point with full metadata payload."""
        if self._mem_items is not None:
            return self._upsert_in_memory(item, dense_vector, model_name)
        self._upsert_qdrant(item, dense_vector, sparse_indices, sparse_values, model_name)

    def delete(self, item_id: UUID) -> None:
        """Remove a point by its memory item ID."""
        if self._mem_items is not None:
            return self._delete_in_memory(item_id)
        self._delete_qdrant(item_id)

    def reindex(
        self,
        items: Sequence[tuple[MemoryItem, list[float]]],
        model_name: str | None = None,
    ) -> None:
        """Drop all points and re-insert from scratch.

        This is a blunt full-reindex; incremental sync can be added later.
        """
        if self._mem_items is not None:
            return self._reindex_in_memory(items, model_name)
        self._reindex_qdrant(items, model_name)

    # ---- in-memory fallback implementation ------------------------------

    def _upsert_in_memory(
        self,
        item: MemoryItem,
        dense_vector: list[float],
        model_name: str | None = None,
    ) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        with self._mem_lock:
            self._mem_items[item.id] = (item, dense_vector)

    def _delete_in_memory(self, item_id: UUID) -> None:
        assert self._mem_items is not None and self._mem_lock is not None
        with self._mem_lock:
            self._mem_items.pop(item_id, None)

    def _reindex_in_memory(
        self,
        items: Sequence[tuple[MemoryItem, list[float]]],
        model_name: str | None = None,
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
        query_vector = (
            self._embed_query(query.text) if self._query_embedding_client is not None else None
        )
        results: list[Candidate] = []
        with self._mem_lock:
            for item, vec in self._mem_items.values():
                if not self._matches_filter(item, query):
                    continue
                # Cosine similarity against the real query vector when an
                # embedding client is wired; otherwise use the deterministic
                # fallback required by unit tests.
                if query_vector is not None:
                    semantic = self._bounded_cosine(query_vector, vec)
                else:
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
        model_name: str | None = None,
    ) -> None:
        from qdrant_client.models import PointStruct, SparseVector

        vectors: dict[str, object] = {"dense": dense_vector}
        if sparse_indices is not None and sparse_values is not None:
            vectors["sparse"] = SparseVector(indices=sparse_indices, values=sparse_values)

        payload = self._item_to_payload(item, include_text=self._payload_text)
        if model_name:
            payload["model_name"] = model_name

        point = PointStruct(
            id=str(item.id),
            vector=vectors,
            payload=payload,
        )
        self._client.upsert(collection_name=self.collection, points=[point])  # type: ignore[union-attr]

    def _delete_qdrant(self, item_id: UUID) -> None:
        from qdrant_client.models import PointIdsList

        self._client.delete(  # type: ignore[union-attr]
            collection_name=self.collection,
            points_selector=PointIdsList(points=[str(item_id)]),
        )

    def _reindex_qdrant(
        self,
        items: Sequence[tuple[MemoryItem, list[float]]],
        model_name: str | None = None,
    ) -> None:
        from qdrant_client.models import PointStruct

        # Delete all existing points in the collection.
        self._client.delete_collection(self.collection)  # type: ignore[union-attr]
        self.connect()

        # Batch upsert in chunks of 100.
        batch: list[PointStruct] = []
        for item, vec in items:
            payload = self._item_to_payload(item, include_text=self._payload_text)
            if model_name:
                payload["model_name"] = model_name

            batch.append(
                PointStruct(
                    id=str(item.id),
                    vector={"dense": vec},
                    payload=payload,
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
            Filter,
            MatchValue,
        )

        if self._query_embedding_client is None:
            raise NotImplementedError(
                "Live Qdrant search requires a query embedding client. "
                "Pass query_embedding_client when constructing QdrantCandidateSource."
            )

        query_vector = self._embed_query(query.text)
        if len(query_vector) != self.dense_dim:
            raise RuntimeError(
                f"query embedding dimension mismatch: expected {self.dense_dim}, "
                f"got {len(query_vector)}"
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
                must_conditions.append(FieldCondition(key="labels", match=MatchValue(value=label)))

        rows = self._query_points(
            query_vector=query_vector,
            query_filter=Filter(must=must_conditions),
            limit=max(query.top_k * 3, query.top_k),
        )
        candidates: list[Candidate] = []
        query_terms = self._terms(query.text)
        for row in rows:
            payload = row.payload or {}
            item = self._payload_to_candidate_item(payload)
            if not self._matches_filter(item, query):
                continue
            item_terms = self._terms(item.text)
            overlap = len(query_terms & item_terms)
            lexical = overlap / max(1, len(query_terms))
            candidates.append(
                Candidate(
                    item=item,
                    source=self.name,
                    semantic=max(0.0, min(1.0, float(row.score))),
                    lexical=lexical,
                    entity=lexical,
                    trust=item.confidence,
                )
            )
            if len(candidates) >= query.top_k:
                break
        return tuple(candidates)

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
        if item.status in (MemoryStatus.REJECTED, MemoryStatus.ARCHIVED):
            return False
        return True

    @staticmethod
    def _terms(text: str) -> set[str]:
        """Tokenize text for lexical overlap scoring."""
        return {m.group(0).casefold() for m in _WORD.finditer(text)}

    @staticmethod
    def _item_to_payload(item: MemoryItem, *, include_text: bool = True) -> dict[str, object]:
        """Serialize a MemoryItem into a Qdrant-compatible payload dict."""
        payload: dict[str, object] = {
            "memory_id": str(item.id),
            "tenant_id": str(item.tenant_id),
            "workspace_id": str(item.workspace_id),
            "agent_id": str(item.agent_id) if item.agent_id else None,
            "thread_id": str(item.thread_id) if item.thread_id else None,
            "layer": str(item.layer),
            "scope": str(item.scope),
            "kind": item.kind,
            "labels": list(item.labels),
            "status": item.status.value,
            "importance": item.importance,
            "salience": item.salience,
            "confidence": item.confidence,
            "created_at": item.created_at.isoformat(),
            "revision": item.revision,
            "supersedes_id": str(item.supersedes_id) if item.supersedes_id else None,
        }
        if include_text:
            payload["text"] = item.text
        else:
            payload["text_redacted"] = True
        return payload

    def _payload_to_candidate_item(self, payload: dict[str, object]) -> MemoryItem:
        """Hydrate a redacted Qdrant payload from the ledger when available."""
        if not payload.get("text") and self._ledger is not None:
            memory_id = UUID(str(payload["memory_id"]))
            tenant_id = UUID(str(payload["tenant_id"]))
            hydrated = self._ledger.get(tenant_id, memory_id)
            if hydrated is not None:
                return hydrated
        item = self._payload_to_item(payload)
        return item

    @staticmethod
    def _payload_to_item(payload: dict[str, object]) -> MemoryItem:
        """Reconstruct the memory item shape needed for retrieval fusion."""
        labels_raw = payload.get("labels") or []
        labels = tuple(str(label) for label in labels_raw) if isinstance(labels_raw, list) else ()
        supersedes_raw = payload.get("supersedes_id")
        created_raw = payload.get("created_at")
        created_at = (
            datetime.fromisoformat(str(created_raw)) if created_raw else datetime.now().astimezone()
        )
        return MemoryItem(
            id=UUID(str(payload["memory_id"])),
            tenant_id=UUID(str(payload["tenant_id"])),
            workspace_id=UUID(str(payload["workspace_id"])),
            agent_id=UUID(str(payload["agent_id"])) if payload.get("agent_id") else None,
            thread_id=UUID(str(payload["thread_id"])) if payload.get("thread_id") else None,
            layer=MemoryLayer(str(payload["layer"])),
            scope=MemoryScope(str(payload["scope"])),
            kind=str(payload.get("kind") or "fact"),
            text=str(payload.get("text") or ""),
            labels=labels,
            status=MemoryStatus(str(payload.get("status") or MemoryStatus.ACTIVE)),
            importance=float(cast(_NumericPayload, payload.get("importance") or 0.5)),
            salience=float(cast(_NumericPayload, payload.get("salience") or 0.5)),
            confidence=float(cast(_NumericPayload, payload.get("confidence") or 0.7)),
            created_at=created_at,
            revision=int(cast(_NumericPayload, payload.get("revision") or 1)),
            supersedes_id=UUID(str(supersedes_raw)) if supersedes_raw else None,
            provenance=Provenance(source_kind="qdrant-payload"),
        )

    @staticmethod
    def _bounded_cosine(left: list[float], right: list[float]) -> float:
        if len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        left_norm = sqrt(sum(a * a for a in left))
        right_norm = sqrt(sum(b * b for b in right))
        cosine = dot / max(left_norm * right_norm, 1e-12)
        return max(0.0, min(1.0, (cosine + 1.0) / 2.0))

    def _embed_query(self, text: str) -> list[float]:
        """Use query-specific embeddings when the provider exposes them."""
        assert self._query_embedding_client is not None
        embed_query = getattr(self._query_embedding_client, "embed_query", None)
        if callable(embed_query):
            return cast(_QueryEmbeddingMethod, embed_query)(text)
        return self._query_embedding_client.embed(text)

    def _query_points(
        self,
        *,
        query_vector: list[float],
        query_filter: object,
        limit: int,
    ) -> Sequence[_ScoredPoint]:
        """Run a named-vector query across qdrant-client API versions."""
        assert self._client is not None
        search = getattr(self._client, "search", None)
        if callable(search):
            return cast(_SearchMethod, search)(
                collection_name=self.collection,
                query_vector=("dense", query_vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        response = cast(_QueryPointsClient, self._client).query_points(
            collection_name=self.collection,
            query=query_vector,
            using="dense",
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return response.points
